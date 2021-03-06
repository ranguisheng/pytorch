from optparse import OptionParser
import yaml
from collections import OrderedDict

import cwrap_parser
import nn_parse
import native_parse
import preprocess_declarations
import function_wrapper
import copy_wrapper

from code_template import CodeTemplate


# This file is the top-level entry point for code generation in ATen.
# It takes an arbitrary number of arguments specifying metadata files to
# process (.cwrap, .yaml and .h) and outputs a number generated header
# and cpp files in ATen/ (see invocations of 'write' for each file that
# is written.) It is invoked from cmake; look for the 'cwrap_files'
# variable for an up-to-date list of files which are passed.


parser = OptionParser()
parser.add_option('-s', '--source-path', help='path to source directory for ATen',
                  action='store', default='.')
parser.add_option('-o', '--output-dependencies',
                  help='only output a list of dependencies', action='store')
parser.add_option('-n', '--no-cuda', action='store_true')

options, files = parser.parse_args()


class FileManager(object):
    def __init__(self):
        self.filenames = set()
        self.outputs_written = False
        self.undeclared_files = []

    def will_write(self, filename):
        filename = "ATen/" + filename
        if self.outputs_written:
            raise Exception("'will_write' can only be called before " +
                            "the call to write_outputs, refactor so outputs are registered " +
                            "before running the generators")
        self.filenames.add(filename)

    def write_outputs(self, filename):
        with open(filename, 'w') as f:
            for name in sorted(self.filenames):
                f.write(name + ";")
        self.outputs_written = True

    def write(self, filename, s):
        filename = "ATen/" + filename
        with open(filename, "w") as f:
            f.write(s)
        if filename not in self.filenames:
            self.undeclared_files.append(filename)
        else:
            self.filenames.remove(filename)

    def check_all_files_written(self):
        if len(self.undeclared_files) > 0:
            raise Exception(
                "trying to write files {} which are not ".format(self.undeclared_files) +
                "in the list of outputs this script produces. " +
                "use will_write to add them.")
        if len(self.filenames) > 0:
            raise Exception("Outputs declared with 'will_write' were " +
                            "never written: {}".format(self.filenames))


TEMPLATE_PATH = options.source_path + "/templates"
GENERATOR_DERIVED = CodeTemplate.from_file(
    TEMPLATE_PATH + "/GeneratorDerived.h")
STORAGE_DERIVED_CPP = CodeTemplate.from_file(
    TEMPLATE_PATH + "/StorageDerived.cpp")
STORAGE_DERIVED_H = CodeTemplate.from_file(TEMPLATE_PATH + "/StorageDerived.h")

TYPE_DERIVED_CPP = CodeTemplate.from_file(TEMPLATE_PATH + "/TypeDerived.cpp")
TYPE_DERIVED_H = CodeTemplate.from_file(TEMPLATE_PATH + "/TypeDerived.h")
TYPE_H = CodeTemplate.from_file(TEMPLATE_PATH + "/Type.h")
TYPE_CPP = CodeTemplate.from_file(TEMPLATE_PATH + "/Type.cpp")

TENSOR_DERIVED_CPP = CodeTemplate.from_file(
    TEMPLATE_PATH + "/TensorDerived.cpp")
TENSOR_SPARSE_CPP = CodeTemplate.from_file(
    TEMPLATE_PATH + "/TensorSparse.cpp")
TENSOR_DENSE_CPP = CodeTemplate.from_file(
    TEMPLATE_PATH + "/TensorDense.cpp")

TENSOR_DERIVED_H = CodeTemplate.from_file(TEMPLATE_PATH + "/TensorDerived.h")
TENSOR_H = CodeTemplate.from_file(TEMPLATE_PATH + "/Tensor.h")
TENSOR_METHODS_H = CodeTemplate.from_file(TEMPLATE_PATH + "/TensorMethods.h")

FUNCTIONS_H = CodeTemplate.from_file(TEMPLATE_PATH + "/Functions.h")

NATIVE_FUNCTIONS_H = CodeTemplate.from_file(TEMPLATE_PATH + "/NativeFunctions.h")

file_manager = FileManager()

generators = {
    'CPUGenerator.h': {
        'name': 'CPU',
        'th_generator': 'THGenerator * generator;',
        'header': 'TH/TH.h',
    },
    'CUDAGenerator.h': {
        'name': 'CUDA',
        'th_generator': '',
        'header': 'THC/THC.h'
    },
}

backends = ['CPU']
if not options.no_cuda:
    backends.append('CUDA')

densities = ['Dense', 'Sparse']

# scalar_name, c_type, accreal, th_scalar_type, is_floating_type
scalar_types = [
    ('Byte', 'uint8_t', 'Long', 'uint8_t', False),
    ('Char', 'int8_t', 'Long', 'int8_t', False),
    ('Double', 'double', 'Double', 'double', True),
    ('Float', 'float', 'Double', 'float', True),
    ('Int', 'int', 'Long', 'int32_t', False),
    ('Long', 'int64_t', 'Long', 'int64_t', False),
    ('Short', 'int16_t', 'Long', 'int16_t', False),
    ('Half', 'Half', 'Double', 'THHalf', True),
]

# shared environment for non-derived base classes Type.h Tensor.h Storage.h
top_env = {
    'type_registrations': [],
    'type_headers': [],
    'type_method_declarations': [],
    'type_method_definitions': [],
    'type_method_inline_definitions': [],
    'tensor_method_declarations': [],
    'tensor_method_definitions': [],
    'function_declarations': [],
    'function_definitions': [],
    'type_ids': [],
    'native_function_declarations': [],
}


def dict_representer(dumper, data):
    return dumper.represent_dict(data.items())


def postprocess_output_declarations(output_declarations):
    # ensure each return has a name associated with it
    for decl in output_declarations:
        has_named_ret = False
        for n, ret in enumerate(decl['returns']):
            if 'name' not in ret:
                assert not has_named_ret
                if decl['inplace']:
                    ret['name'] = 'self'
                elif len(decl['returns']) == 1:
                    ret['name'] = 'result'
                else:
                    ret['name'] = 'result' + str(n)
            else:
                has_named_ret = True

    return output_declarations


def format_yaml(data):
    if options.output_dependencies:
        # yaml formatting is slow so don't do it if we will ditch it.
        return ""
    noalias_dumper = yaml.dumper.SafeDumper
    noalias_dumper.ignore_aliases = lambda self, data: True
    # Support serializing OrderedDict
    noalias_dumper.add_representer(OrderedDict, dict_representer)
    return yaml.dump(data, default_flow_style=False, Dumper=noalias_dumper)


def generate_storage_type_and_tensor(backend, density, scalar_type, declarations):
    scalar_name, c_type, accreal, th_scalar_type, is_floating_type = scalar_type
    env = {}
    density_tag = 'Sparse' if density == 'Sparse' else ''
    th_density_tag = 'S' if density == 'Sparse' else ''
    env['Density'] = density
    env['ScalarName'] = scalar_name
    env['ScalarType'] = c_type
    env['THScalarType'] = th_scalar_type
    env['AccScalarName'] = accreal
    env['isFloatingType'] = is_floating_type
    env['isIntegralType'] = not is_floating_type
    env['Storage'] = "{}{}Storage".format(backend, scalar_name)
    env['Type'] = "{}{}{}Type".format(density_tag, backend, scalar_name)
    env['Tensor'] = "{}{}{}Tensor".format(density_tag, backend, scalar_name)
    env['DenseTensor'] = "{}{}Tensor".format(backend, scalar_name)
    env['SparseTensor'] = "Sparse{}{}Tensor".format(backend, scalar_name)
    env['Backend'] = density_tag + backend
    env['DenseBackend'] = backend

    # used for generating switch logic for external functions
    tag = density_tag + backend + scalar_name
    env['TypeID'] = 'TypeID::' + tag
    top_env['type_ids'].append(tag + ',')

    if backend == 'CUDA':
        env['th_headers'] = ['#include <THC/THC.h>',
                             '#include <THCUNN/THCUNN.h>',
                             '#undef THNN_',
                             '#undef THCIndexTensor_']
        # if density == 'Sparse':
        env['th_headers'] += ['#include <THCS/THCS.h>',
                              '#undef THCIndexTensor_']
        sname = '' if scalar_name == "Float" else scalar_name
        env['THType'] = 'Cuda{}'.format(sname)
        env['THStorage'] = 'THCuda{}Storage'.format(sname)
        if density == 'Dense':
            env['THTensor'] = 'THCuda{}Tensor'.format(sname)
        else:
            env['THTensor'] = 'THCS{}Tensor'.format(scalar_name)
        env['THIndexTensor'] = 'THCudaLongTensor'
        env['state'] = ['context->thc_state']
        env['isCUDA'] = 'true'
        env['storage_device'] = 'return storage->device;'
        env['Generator'] = 'CUDAGenerator'
    else:
        env['th_headers'] = ['#include <TH/TH.h>',
                             '#include <THNN/THNN.h>',
                             '#undef THNN_']
        # if density == 'Sparse':
        env['th_headers'].append('#include <THS/THS.h>')

        env['THType'] = scalar_name
        env['THStorage'] = "TH{}Storage".format(scalar_name)
        env['THTensor'] = 'TH{}{}Tensor'.format(th_density_tag, scalar_name)
        env['THIndexTensor'] = 'THLongTensor'
        env['state'] = []
        env['isCUDA'] = 'false'
        env['storage_device'] = 'throw std::runtime_error("CPU storage has no device");'
        env['Generator'] = 'CPUGenerator'
    env['AS_REAL'] = env['ScalarType']
    if scalar_name == "Half":
        env['SparseTensor'] = 'Tensor'
        if backend == "CUDA":
            env['to_th_type'] = 'HalfFix<__half,Half>'
            env['to_at_type'] = 'HalfFix<Half,__half>'
            env['AS_REAL'] = 'convert<half,double>'
            env['THScalarType'] = 'half'
        else:
            env['to_th_type'] = 'HalfFix<THHalf,Half>'
            env['to_at_type'] = 'HalfFix<Half,THHalf>'
    elif scalar_name == 'Long':
        env['to_th_type'] = 'long'
        env['to_at_type'] = 'int64_t'
    else:
        env['to_th_type'] = ''
        env['to_at_type'] = ''

    declarations, definitions = function_wrapper.create_derived(
        env, declarations)
    env['type_derived_method_declarations'] = declarations
    env['type_derived_method_definitions'] = definitions

    if density != 'Sparse':
        # there are no special storage types for Sparse, they are composed
        # of Dense tensors
        file_manager.write(env['Storage'] + ".cpp", STORAGE_DERIVED_CPP.substitute(env))
        file_manager.write(env['Storage'] + ".h", STORAGE_DERIVED_H.substitute(env))
        env['TensorDenseOrSparse'] = TENSOR_DENSE_CPP.substitute(env)
        env['THTensor_nDimension'] = 'tensor->nDimension'
    else:
        env['TensorDenseOrSparse'] = TENSOR_SPARSE_CPP.substitute(env)
        env['THTensor_nDimension'] = 'tensor->nDimensionI + tensor->nDimensionV'

    file_manager.write(env['Type'] + ".cpp", TYPE_DERIVED_CPP.substitute(env))
    file_manager.write(env['Type'] + ".h", TYPE_DERIVED_H.substitute(env))

    file_manager.write(env['Tensor'] + ".cpp", TENSOR_DERIVED_CPP.substitute(env))
    file_manager.write(env['Tensor'] + ".h", TENSOR_DERIVED_H.substitute(env))

    type_register = (('context->type_registry[static_cast<int>(Backend::{})]' +
                      '[static_cast<int>(ScalarType::{})].reset(new {}(context));')
                     .format(env['Backend'], scalar_name, env['Type']))
    top_env['type_registrations'].append(type_register)
    top_env['type_headers'].append(
        '#include "ATen/{}.h"'.format(env['Type']))

    return env


def iterate_types():
    for backend in backends:
        for density in densities:
            for scalar_type in scalar_types:
                if density == 'Sparse' and scalar_type[0] == 'Half':
                    # THS does not do half type yet.
                    continue
                yield (backend, density, scalar_type)


###################
# declare what files will be output _before_ we do any work
# so that the script runs quickly when we are just querying the
# outputs
def declare_outputs():
    files = ['Declarations.yaml', 'Type.h', 'Type.cpp', 'Tensor.h',
             'TensorMethods.h', 'Functions.h',
             'Copy.cpp', 'NativeFunctions.h']
    for f in files:
        file_manager.will_write(f)
    for fname in sorted(generators.keys()):
        file_manager.will_write(fname)
    for backend, density, scalar_types in iterate_types():
        scalar_name = scalar_types[0]
        full_backend = "Sparse" + backend if density == "Sparse" else backend
        for kind in ["Storage", "Type", "Tensor"]:
            if kind == 'Storage' and density == "Sparse":
                continue
            file_manager.will_write("{}{}{}.h".format(full_backend, scalar_name, kind))
            file_manager.will_write("{}{}{}.cpp".format(full_backend, scalar_name, kind))


def generate_outputs():
    cwrap_files = [f for f in files if f.endswith('.cwrap')]
    nn_files = [f for f in files if f.endswith('nn.yaml') or f.endswith('.h')]
    native_files = [f for f in files if f.endswith('native_functions.yaml')]

    declarations = [d
                    for file in cwrap_files
                    for d in cwrap_parser.parse(file)]

    declarations += nn_parse.run(nn_files)
    declarations += native_parse.run(native_files)
    declarations = preprocess_declarations.run(declarations)
    for fname, env in generators.items():
        file_manager.write(fname, GENERATOR_DERIVED.substitute(env))

    # note: this will fill in top_env['type/tensor_method_declarations/definitions']
    # and modify the declarations to include any information that will all_backends
    # be used by function_wrapper.create_derived
    output_declarations = function_wrapper.create_generic(top_env, declarations)
    output_declarations = postprocess_output_declarations(output_declarations)
    file_manager.write("Declarations.yaml", format_yaml(output_declarations))

    # populated by generate_storage_type_and_tensor
    all_types = []

    for backend, density, scalar_type in iterate_types():
        all_types.append(generate_storage_type_and_tensor(
            backend, density, scalar_type, declarations))

    file_manager.write('Type.h', TYPE_H.substitute(top_env))
    file_manager.write('Type.cpp', TYPE_CPP.substitute(top_env))

    file_manager.write('Tensor.h', TENSOR_H.substitute(top_env))
    file_manager.write('TensorMethods.h', TENSOR_METHODS_H.substitute(top_env))
    file_manager.write('Functions.h', FUNCTIONS_H.substitute(top_env))

    file_manager.write('Copy.cpp', copy_wrapper.create(all_types))
    file_manager.write('NativeFunctions.h', NATIVE_FUNCTIONS_H.substitute(top_env))

    file_manager.check_all_files_written()


declare_outputs()
if options.output_dependencies is not None:
    file_manager.write_outputs(options.output_dependencies)
else:
    generate_outputs()
