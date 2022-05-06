from abc import ABC
from dataclasses import dataclass
from typing import Any, Dict, List, Union
from torchgen.context import method_with_native_function
from torchgen.model import (
    BackendIndex,
    NativeFunction,
    NativeFunctionsGroup,
    FunctionSchema,
)
from torchgen.api.types import (
    BaseCType,
    OptionalCType,
    VectorCType,
    kernel_signature,
)
import torchgen.api.dispatcher as dispatcher
from torchgen.api.lazy import (
    LazyIrSchema,
    LazyArgument,
    getValueT,
    isValueType,
    tensorListValueT,
)
from torchgen.dest.lazy_ts_lowering import ts_lowering_body


def node_ctor_arg_rvalue_string(arg: LazyArgument) -> str:
    """
    Given a LazyArgument,
    generate a c++ string for materializing an rvalue of that arg for passing into
    a lazy Node constructor.
    """

    if isValueType(arg.lazy_type):
        if isinstance(arg.lazy_type, BaseCType):
            if arg.is_wrapped_scalar:
                return f"node_{arg.name}"
            elif arg.lazy_type.type is tensorListValueT:
                return f"lazy_{arg.name}_tensorlist"
            elif arg.is_symint_or_list:
                cpp_type = arg.lazy_type.cpp_type()
                return (
                    f"{cpp_type}(std::dynamic_pointer_cast<torch::lazy::SymbolicIntNode>"
                    f"({arg.name}.toSymbolicIntNode())->node_, 0)"
                )
            return f"lazy_{arg.name}->GetIrValue()"
        elif isinstance(arg.lazy_type, OptionalCType):
            if arg.is_wrapped_scalar:
                return f"node_{arg.name}"
            return (
                f"lazy_{arg.name} ? "
                f"c10::make_optional(lazy_{arg.name}->GetIrValue()) : "
                "c10::nullopt"
            )
        else:
            raise AssertionError(
                f"TODO not sure if there are other valid types to handle here ({arg.lazy_type})"
            )
    else:
        if isinstance(arg.lazy_type, VectorCType) and isinstance(
            arg.lazy_type.elem, BaseCType
        ):
            return f"std::vector<{arg.lazy_type.elem.type}>({arg.name}.begin(), {arg.name}.end())"
        elif (
            isinstance(arg.lazy_type, OptionalCType)
            and isinstance(arg.lazy_type.elem, VectorCType)
            and isinstance(arg.lazy_type.elem.elem, BaseCType)
        ):
            return f"torch::lazy::ToOptionalVector<{arg.lazy_type.elem.elem.type}>({arg.name})"
        else:
            return f"{arg.name}"


def node_ctor_inputs(schema: LazyIrSchema) -> str:
    """
    Produce a formatted string with the arguments as passed into the constructor of a node class.
    """
    node_ctor_values = [
        node_ctor_arg_rvalue_string(arg) for arg in schema.filtered_args()
    ]
    return ", ".join(node_ctor_values)


def gen_fallback_code(schema: LazyIrSchema, overload_name: str) -> str:
    """
    Generate code that falls back to eager conditioned on a predicate
    """
    fallback_args = ",\n                ".join(
        [str(arg.name) for arg in schema.filtered_args(generator=True)]
    )
    if len(overload_name):
        aten_op_str = f"ATEN_OP2({schema.aten_name}, {overload_name})"
    else:
        aten_op_str = f"ATEN_OP({schema.aten_name})"
    or_has_generator = ""
    if schema.generator_arg:
        # generators are always optional and there is never more than one, at least currently
        or_has_generator = f" || ({schema.generator_arg.name}.has_value() && {schema.generator_arg.name}->defined())"
    return f"""
        if (force_eager_fallback({aten_symbol(schema)}){or_has_generator}) {{
            return at::native::call_fallback_fn<&ltc_eager_fallback, {aten_op_str}>::call(
                {fallback_args}
            );
        }}
"""


def aten_symbol(schema: LazyIrSchema) -> str:
    missing_interned_strings = {
        "sigmoid_backward",
    }
    if schema.aten_name in missing_interned_strings:
        return f'c10::Symbol::fromQualString("aten::{schema.aten_name}")'

    if not schema.aten_name.startswith("at::"):
        return f"at::aten::{schema.aten_name}"
    else:
        return schema.aten_name


@dataclass(frozen=True)
class GenLazyIR(ABC):
    backend_index: BackendIndex
    backend_name: str
    node_base: str

    @method_with_native_function
    def __call__(self, f: Union[NativeFunctionsGroup, NativeFunction]) -> List[str]:
        func = f.functional.func if isinstance(f, NativeFunctionsGroup) else f.func
        schema = LazyIrSchema(func)
        return self.gen(schema)

    # there is no lowering functionality generated unless this IR base class is subclassed and
    # implemented as a backend-specific node
    def lowering_function(
        self,
        schema: LazyIrSchema,
        declaration_only: bool = False,
    ) -> str:
        return ""

    def can_be_reused_function(
        self, schema: LazyIrSchema, node_ctor_args: str
    ) -> str:
        return ""

    def can_be_reused_function(
        self, f: Union[NativeFunctionsGroup, NativeFunction], node_ctor_args: str
    ) -> str:
        return f"""bool CanBeReused({node_ctor_args}) const {{
    return false;
    }}"""

    def node_base_ctor_call(self, schema: LazyIrSchema) -> str:
        value_args = schema.filtered_args(values=True, scalars=False)
        # backends can customize the way the node base class constructor is called,
        # as long as all of its arguments can be generated from information available from the schema
        base_ctor_value_args_list = []
        for arg in value_args:
            if isinstance(arg.lazy_type, BaseCType) or isinstance(
                arg.lazy_type, VectorCType
            ):
                base_ctor_value_args_list.append(f"{arg.name}")
            elif isinstance(arg.lazy_type, OptionalCType):
                base_ctor_value_args_list.append(f"{arg.name}.value_or(kNullValue)")
            else:
                raise AssertionError(
                    f"Unsupported type ({arg.lazy_type}) - add support if necessary"
                )
        base_ctor_value_args = ", ".join(base_ctor_value_args_list)

        scalar_args = schema.filtered_args(values=False, scalars=True)

        # Shape constuction.
        # Conditionally build shape depending on whether it is a native function
        # and whether the shape cache should be used
        shape_ctor_arg = "std::move(shapes),"
        if not getattr(schema, "has_shape", True):
            shape_ctor_arg = ""
        elif getattr(schema, "is_non_native", False):
            if getattr(schema, "cache_shape", True):
                shape_args = [f"operand({i})" for i in range(len(value_args))]
                shape_ctor_arg = (
                    f"[&](){{{{ return compute_shape_{schema.name}({{}})[0]; }}}},"
                )
            else:
                shape_args = [a.name for a in value_args]
                shape_ctor_arg = f"compute_shape_{schema.name}({{}}),"
            shape_args.extend(a.name for a in scalar_args)
            shape_ctor_arg = shape_ctor_arg.format(", ".join(shape_args))

        scalar_hashes = ", ".join(f"{a.name}" for a in scalar_args)

        return f"""{self.node_base}(
              {schema.node_name}::class_op_kind,
              OpList{{{base_ctor_value_args}}},
              {shape_ctor_arg}
              /* num_outputs */ {len(schema.returns)},
              torch::lazy::MHash({scalar_hashes}))"""

    def gen(self, schema: LazyIrSchema) -> List[str]:
        opkind = getattr(schema, "opkind", aten_symbol(schema))

        # for now, we just want one IR class decl and soon after also the method defs
        # and we use the functional version not out/inplace.
        all_args = schema.filtered_args()
        value_args = schema.filtered_args(values=True, scalars=False)
        scalar_args = schema.filtered_args(values=False, scalars=True)

        ctor_args = [f"const {i.lazy_type.cpp_type()}& {i.name}" for i in all_args]
        if not getattr(schema, "is_non_native", False):
            ctor_args.append("std::vector<torch::lazy::Shape>&& shapes")
        node_ctor_args = ", ".join(ctor_args)

        scalar_initializers = ",\n        ".join(
            f"{a.name}({a.name})" for a in scalar_args
        )
        if len(scalar_initializers):
            scalar_initializers = f",\n        {scalar_initializers}"
        scalar_decls = "\n  ".join(
            [
                f"std::string {a.name};"
                if a.lazy_type.cpp_type() == "c10::string_view"
                else f"{a.lazy_type.cpp_type()} {a.name};"
                for a in scalar_args
            ]
        )
        optional_values = [
            arg.name
            for arg in schema.filtered_args(values=True, scalars=False)
            if isinstance(arg.lazy_type, OptionalCType)
        ]
        has_optional_decls = "\n  ".join(
            [f"bool has_{value}: 1;" for value in optional_values]
        )
        has_optional_defs = "\n    ".join(
            [f"has_{value} = !!{value};" for value in optional_values]
        )
        members_to_string = []
        for arg in scalar_args:
            if isinstance(arg.lazy_type, OptionalCType):
                members_to_string.append(
                    f"""if ({arg.name}.has_value()) {{
      ss << ", {arg.name}=" << {arg.name}.value();
    }} else {{
      ss << ", {arg.name}=null";
    }}"""
                )
            else:
                members_to_string.append(f'ss << ", {arg.name}=" << {arg.name};')
        members_to_string_str = "\n    ".join(members_to_string)

        lowering_function = ""
        if getattr(schema, "is_lowerable", True):
            lowering_function = self.lowering_function(
                schema,
                declaration_only=getattr(schema, "lower_declaration_only", False),
            )

        return [
            f"""\
class {schema.node_name} : public {self.node_base} {{
 public:
  static torch::lazy::OpKind ClassOpKind() {{
    return torch::lazy::OpKind({opkind});
  }}

  {schema.node_name}({node_ctor_args})
      : {self.node_base_ctor_call(schema)}{scalar_initializers}
  {{
    {has_optional_defs}
  }}

  std::string ToString() const override {{
    std::stringstream ss;
    ss << {self.node_base}::ToString();
    {members_to_string_str}
    return ss.str();
  }}

  {self.can_be_reused_function(schema, node_ctor_args)}

  {lowering_function}

  {scalar_decls}
  {has_optional_decls}

}};

""",
        ]


@dataclass(frozen=True)
class GenTSLazyIR(GenLazyIR):
    def lowering_function(
        self, schema: LazyIrSchema, declaration_only: bool = False
    ) -> str:
        if declaration_only:
            lowering_body = ";"
        else:
            lowering_body = f"""
  {{
    {ts_lowering_body(schema)}
  }}
            """

        return f"""
  torch::lazy::TSOpVector Lower(
      std::shared_ptr<torch::jit::GraphFunction> function,
      torch::lazy::TSLoweringContext* loctx) const override {lowering_body}
        """

    def can_be_reused_function(
        self, schema: LazyIrSchema, node_ctor_args: str
    ) -> str:
        value_comparsion = []
        for arg in schema.positional_values:
            if isinstance(arg.lazy_type, OptionalCType):
                value_comparsion.append(
                    f"operand(i++) == {arg.name}.value_or(kNullValue)"
                )
            else:
                value_comparsion.append(f"operand(i++) == {arg.name}")
        for arg in schema.positional_scalars:
            value_comparsion.append(f"this->{arg.name} == {arg.name}")
        for arg in schema.keyword_values:
            value_comparsion.append(f"operand(i++) == {arg.name}")
        for arg in schema.keyword_scalars:
            value_comparsion.append(f"this->{arg.name} == {arg.name}")
        value_comparsion_str = " &&\n        ".join(value_comparsion)

        return f"""bool CanBeReused({node_ctor_args}) const {{
    size_t i = 0;
    return ({value_comparsion_str});
  }}"""


@dataclass(frozen=True)
class GenLazyNativeFuncDefinition:
    class_method_name: str
    backend_index: BackendIndex
    tensor_class: str
    gen_forced_fallback_code: bool
    backend_namespace: str
    get_tensorlist: str
    get_tensor_or_wrap_number: str
    try_get_tensor: str
    metrics_counter: str
    create_tensor: str
    create_from_first_tensor: bool
    create_aten_from_ltc_tensor: str
    tuple_aten_from_ltc_tensors: str
    lazy_tensor_ptr: str
    get_device_fn: str

    def lazy_tensor_decls(self, func: NativeFunction, schema: LazyIrSchema) -> str:
        value_args = schema.filtered_args(values=True, scalars=False)
        # Generates lazy_{name} variables for LazyTensors wrapping input tensors
        lazy_tensor_decls: List[str] = []
        for arg in value_args:
            if arg.is_wrapped_scalar:
                if isinstance(arg.lazy_type, OptionalCType):
                    lazy_tensor_decls.append(
                        f"""auto node_{arg.name} = {arg.name} ?
                c10::make_optional(torch::lazy::LazyGraphExecutor::Get()->GetIrValueForScalarFromCodegen(*{arg.name})):
                c10::nullopt;"""
                    )
                else:
                    lazy_tensor_decls.append(
                        f"""auto node_{arg.name} =
                torch::lazy::LazyGraphExecutor::Get()->GetIrValueForScalarFromCodegen({arg.name});"""
                    )
            elif arg.is_symint_or_list:
                continue  # values are extracted in isValueType
            elif isinstance(arg.lazy_type, BaseCType):
                if arg.lazy_type.type is tensorListValueT:
                    lazy_tensor_decls.append(
                        f"auto lazy_{arg.name}_tensorlist = "
                        f"{self.backend_namespace}::{self.get_tensorlist}({arg.name});"
                    )
                else:
                    lazy_tensor_decls.append(
                        f"{self.lazy_tensor_ptr} lazy_{arg.name} = "
                        f"{self.backend_namespace}::{self.get_tensor_or_wrap_number}({arg.name}, *common_device);"
                    )
            elif isinstance(arg.lazy_type, OptionalCType):
                # TODO(alanwaketan): Maybe we want to apply GetLtcTensorOrCreateForWrappedNumber here, but hold it
                # until we encounter a real world example.
                lazy_tensor_decls.append(
                    f"{self.lazy_tensor_ptr} lazy_{arg.name} = "
                    f"{self.backend_namespace}::{self.try_get_tensor}({arg.name}.value_or(at::Tensor()));"
                )
            else:
                raise AssertionError(
                    f"TODO not sure if there are other valid types to handle here ({arg.lazy_type})"
                )
        return ("\n        ").join(lazy_tensor_decls)

    def force_eager_fallback(self, func: NativeFunction, schema: LazyIrSchema) -> str:
        if self.gen_forced_fallback_code:
            return gen_fallback_code(schema, overload_name=func.func.name.overload_name)
        return ""

    def metrics(self, func: NativeFunction, schema: LazyIrSchema) -> str:
        return f"{self.metrics_counter};"

    def get_device(self, func: NativeFunction, schema: LazyIrSchema) -> str:
        value_args = schema.filtered_args(values=True, scalars=False)
        value_types_names = [f"{a.name}" for a in value_args if not a.is_wrapped_scalar]
        assert (
            len(value_types_names) > 0
        ), "Code below assumes there is at least one tensor arg"
        return f"""auto common_device = {self.get_device_fn}({', '.join(value_types_names)});
        TORCH_INTERNAL_ASSERT(common_device);
        """

    def shape_inference(self, func: NativeFunction, schema: LazyIrSchema) -> str:
        metadata = self.backend_index.get_kernel(func)
        assert metadata is not None
        all_args = schema.filtered_args()
        returns_length = len(schema.returns)
        # call the meta kernel if it exists, to compute output shape/dtype for our IR
        if func.structured or func.structured_delegate is not None:
            meta_out = """std::vector<torch::lazy::Shape> shapes{
        torch::lazy::Shape(out_meta.scalar_type(), out_meta.sizes().vec())};"""
            if returns_length > 1:

                def this_shape(i: int) -> str:
                    return f"torch::lazy::Shape(std::get<{i}>(out_meta).scalar_type(), std::get<{i}>(out_meta).sizes().vec())"

                shapes_str = ",".join([this_shape(i) for i in range(returns_length)])
                meta_out = "std::vector<torch::lazy::Shape> shapes{" + shapes_str + "};"

            shape_str = f"""auto out_meta = at::meta::{schema.aten_name}({', '.join(str(a.name) for a in all_args)});
            {meta_out}"""
        else:
            shape_sig = ComputeShapeSignature(metadata.kernel, func)
            shape_str = f"""
            auto shapes = {shape_sig.shape_call};"""

        shape_str += f"""
            TORCH_INTERNAL_ASSERT(shapes.size() == {returns_length});"""

        # Calculating which dimensions are symbolic
        func_schema_str = "aten::" + str(func.func)
        shape_str += f"""
            if(torch::lazy::symbolicShapeEnabled()){{
                std::vector<torch::jit::IValue> inputs = {{ {', '.join(str(a.name) for a in all_args)} }};
                const char* schema_str = "{func_schema_str}";
                applySymbolicShapesOnLT(schema_str, inputs, shapes);
            }}
        """
        return shape_str

    def build_ir_node(self, func: NativeFunction, schema: LazyIrSchema) -> str:
        node_ctor_input_str = node_ctor_inputs(schema)
        return f"""torch::lazy::NodePtr node = torch::lazy::ReuseNode<{schema.node_name}>({node_ctor_input_str});
        if (!node) {{
            {self.shape_inference(func, schema)}
            node = torch::lazy::MakeNode<{schema.node_name}>({node_ctor_input_str}, std::move(shapes));
            CacheNode(node);
        }}
        """

    def create_lazy_tensor(self, first_tensor_name: str) -> str:
        # xla uses an instance method for tensor creation, for the time being
        if self.create_from_first_tensor:
            # TODO(whc) remove this if XLA switches to using static method for creation
            return f"{first_tensor_name}.{self.create_tensor}"
        return f"{self.backend_namespace}::{self.create_tensor}"

    def return_aten_tensor(self, func: NativeFunction, schema: LazyIrSchema) -> str:
        returns_length = len(schema.returns)
        value_args = schema.filtered_args(values=True, scalars=False)
        value_types_names = [f"{a.name}" for a in value_args if not a.is_wrapped_scalar]
        assert (
            len(value_types_names) > 0
        ), "Code below assumes there is at least one tensor arg"
        first_tensor_name = value_types_names[0]
        bridge_str = f"""auto result = {self.create_aten_from_ltc_tensor}(
                {self.create_lazy_tensor(first_tensor_name)}(std::move(node), *common_device));"""

        if returns_length > 1:
            bridge_str = f"""std::vector<{self.lazy_tensor_ptr}> lazy_tensors;
        for (int i = 0; i < {returns_length}; i++) {{
            lazy_tensors.push_back({self.create_lazy_tensor(first_tensor_name)}({getValueT()}(node, i), *common_device));
        }}
        auto result = {self.tuple_aten_from_ltc_tensors}<{returns_length}>(lazy_tensors);"""

        if schema.name.name.inplace or func.func.is_out_fn():
            assert returns_length == 1, (
                "We assumed there was no such case where an op is an in-place variant "
                f"and has tuple outputs, but got tuple of len {returns_length}."
            )
            bridge_str = f"""lazy_{first_tensor_name}->SetInPlaceIrValue(node);
        auto& result = {first_tensor_name};"""

        bridge_str += """
        return result;"""
        return bridge_str

    @method_with_native_function
    def __call__(self, func: NativeFunction) -> List[str]:
        sig = kernel_signature(func, self.backend_index)
        metadata = self.backend_index.get_kernel(func)
        assert metadata is not None
        schema = LazyIrSchema(func.func)
        return [
            f"""\
    {sig.decl(name=f"{self.class_method_name}::{metadata.kernel}")} {{
        {self.force_eager_fallback(func, schema)}
        {self.metrics(func, schema)}
        {self.get_device(func, schema)}
        {self.lazy_tensor_decls(func, schema)}
        {self.build_ir_node(func, schema)}
        {self.return_aten_tensor(func, schema)}
    }};\n
    """
        ]


class ComputeShapeSignature:
    """
    Here we use the base name as the suffix of the signature to avoid generating for in-place variants.
    """

    def __init__(self, kernel_name: str, f: NativeFunction):
        self.__schema = LazyIrSchema(f.func)
        self.__dispatch_args = ", ".join(
            [a.decl() for a in dispatcher.arguments(f.func)]
        )
        self.__call_args = ", ".join(
            [f"{arg.name}" for arg in self.__schema.filtered_args(generator=True)]
        )
        self.__kernel_name = kernel_name

    def __decl_suffix(self) -> str:
        return f"{self.__kernel_name}({self.__dispatch_args})"

    def __call_suffix(self) -> str:
        return f"{self.__kernel_name}({self.__call_args})"

    @property
    def shape_decl(self) -> str:
        return f"TORCH_API std::vector<torch::lazy::Shape> compute_shape_{self.__decl_suffix()}"

    @property
    def shape_call(self) -> str:
        return f"torch::lazy::compute_shape_{self.__call_suffix()}"


@dataclass(frozen=True)
class GenLazyShapeInferenceDefinition:
    backend_index: BackendIndex
    tensor_class: str

    @method_with_native_function
    def __call__(self, f: NativeFunction) -> List[str]:
        sig = kernel_signature(f, self.backend_index)
        metadata = self.backend_index.get_kernel(f)
        assert metadata is not None

        # Only generate shape/dtype fn for non-structured kernels,
        # since we just use the meta function for structured kernels
        if not f.structured and f.structured_delegate is None:
            shape_sig = ComputeShapeSignature(metadata.kernel, f)
            return ["\n".join([f"{shape_sig.shape_decl};"])]
        else:
            return []


def generate_non_native_lazy_ir_nodes(
    non_native: List[Dict[str, Any]], gen_lazy_ir: GenLazyIR
) -> List[str]:
    """Generate the non-native lazy IR node classes"""
    nodes = []
    for op in non_native:
        schema = LazyIrSchema(FunctionSchema.parse(op["func"]))
        schema.is_non_native = True
        opkind = op.get("opkind", None)
        if opkind:
            schema._aten_name = opkind
            if not opkind.startswith("at::"):
                schema.ltc_name = opkind
        schema.has_shape = op.get("has_shape", True)
        schema.cache_shape = op.get("cache_shape", True)
        schema.is_lowerable = op.get("is_lowerable", False)
        schema.lower_declaration_only = op.get("lower_declaration_only", True)

        nodes.append(gen_lazy_ir.gen(schema)[0])

    return nodes
