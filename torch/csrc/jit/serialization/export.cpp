#include <torch/csrc/jit/serialization/export.h>

#include <ATen/ATen.h>
#include <ATen/Utils.h>
#include <ATen/core/functional.h>
#include <c10/util/Exception.h>
#include <c10/util/Optional.h>
#include <c10/util/accumulate.h>
#include <c10/util/irange.h>
#include <torch/csrc/autograd/symbolic.h>
#include <torch/csrc/jit/jit_log.h>
#include <torch/csrc/jit/passes/dead_code_elimination.h>
#include <torch/csrc/jit/passes/inliner.h>
#include <torch/csrc/jit/runtime/instruction.h>
#include <torch/csrc/jit/serialization/import_export_constants.h>
#include <torch/csrc/jit/serialization/import_export_functions.h>
#include <torch/csrc/jit/serialization/import_export_helpers.h>
#include <torch/csrc/jit/serialization/onnx.h>
#include <torch/csrc/onnx/onnx.h>

#include <onnx/checker.h>
#include <onnx/onnx_pb.h>
#include <onnx/proto_utils.h>

#include <fstream>
#include <memory>
#include <regex>
#include <set>
#include <string>
#include <vector>

namespace torch {
namespace jit {

void writeArchiveAndTensors(
    const std::string& archive_name,
    const char* data,
    size_t size,
    const std::vector<at::Tensor>& tensors,
    caffe2::serialize::PyTorchStreamWriter& out) {
  std::string prefix = archive_name + "/";
  size_t i = 0;
  for (const auto& td : tensors) {
    WriteableTensorData writable_td = getWriteableTensorData(td);
    std::string fname = prefix + std::to_string(i++);
    out.writeRecord(fname, writable_td.data(), writable_td.sizeInBytes());
  }
  std::string fname = archive_name + ".pkl";
  out.writeRecord(fname, data, size);
}

namespace {
namespace onnx_torch = ::torch::onnx;
namespace onnx = ::ONNX_NAMESPACE;

std::string getNodeStackTraceString(const Node* n) {
  return n->sourceRange().str();
}

void validateBlock(
    Block* b,
    onnx_torch::OperatorExportTypes operator_export_type) {
  for (auto node : b->nodes()) {
    for (Block* sub_block : node->blocks()) {
      validateBlock(sub_block, operator_export_type);
    }
    // Macro'ed so we get a marginally better line number on failed export
#define FAIL_EXPORT(name)                          \
  throw std::runtime_error(                        \
      std::string("ONNX export failed: ") + name + \
      "\n\nGraph we tried to export:\n" + b->owningGraph()->toString());
    // Special error messages for certain types of operators
    if (node->kind() == prim::PythonOp) {
      auto py_node = static_cast<PythonOp*>(node);
      FAIL_EXPORT(
          "Couldn't export Python operator " + py_node->name() +
          "\n\nDefined at:\n" + getNodeStackTraceString(node))
    } else {
      if (node->kind() == aten::expand) {
        if (operator_export_type ==
            onnx_torch::OperatorExportTypes::ONNX_ATEN_FALLBACK) {
          WithInsertPoint guard(node);
          auto* new_node =
              b->owningGraph()->insertNode(b->owningGraph()->create(
                  Symbol(::c10::onnx::ATen),
                  node->inputs(),
                  node->outputs().size()));
          for (size_t i = 0; i < node->outputs().size(); ++i) {
            node->output(i)->replaceAllUsesWith(new_node->output(i));
          }
          new_node->s_(Symbol::fromQualString("attr::operator"), "expand");
        }
      }
      if (node->kind() == prim::PackPadded || node->kind() == prim::PadPacked) {
        if (operator_export_type !=
            onnx_torch::OperatorExportTypes::ONNX_FALLTHROUGH) {
          FAIL_EXPORT(
              "Cannot export individual pack_padded_sequence or pad_packed_sequence; these operations must occur in pairs.\n\nUsage of this operation occurred at:\n" +
              getNodeStackTraceString(node));
        }
      }
      bool is_aten_enabled = operator_export_type ==
              onnx_torch::OperatorExportTypes::ONNX_ATEN_FALLBACK ||
          operator_export_type == onnx_torch::OperatorExportTypes::ONNX_ATEN ||
          operator_export_type ==
              onnx_torch::OperatorExportTypes::ONNX_FALLTHROUGH;
      if (node->kind().is_aten() && !is_aten_enabled && !node->mustBeNone()) {
        FAIL_EXPORT(
            "Couldn't export operator " + node->kind().toDisplayString() +
            "\n\nDefined at:\n" + getNodeStackTraceString(node));
      }
    }
#undef FAIL_EXPORT
  }
}

void validateGraph(
    const std::shared_ptr<Graph>& graph,
    onnx_torch::OperatorExportTypes operator_export_type) {
  validateBlock(graph->block(), operator_export_type);
}

std::string GetFileRootPath(const std::string& rootPath) {
  std::string rootPath_ = rootPath;
  // First, making slash consistent.
  std::replace(rootPath_.begin(), rootPath_.end(), '\\', '/');
  // Second, remove trailing slashes, if any
  std::regex trailer("/+$");
  std::string root = std::regex_replace(rootPath_, trailer, std::string());
  std::string folder = root.substr(0, root.find_last_of('/'));
  if (folder == rootPath_) { // If no root folder specified, select cwd.
    return std::string(".");
  }
  return folder;
}

std::string GetExternalFileName(
    const c10::optional<std::string>& external_ref) {
  auto tensorName = external_ref.value();
  const std::string illegalChars = "\\/:?\"<>|";
  for (char& i : tensorName) {
    if (illegalChars.find(i) != std::string::npos) {
      i = '_';
    }
  }
  return tensorName;
}

void CloseFile(FILE* fp) {
  fclose(fp);
}

void CreateExternalFile(
    const at::Tensor& tensor,
    const std::string& tensorName,
    const std::string& onnx_file_path) {
  auto folder = GetFileRootPath(onnx_file_path);
  std::string fullFilePath = folder + "/" + tensorName;
  std::unique_ptr<FILE, decltype(&CloseFile)> fp(
      fopen(fullFilePath.c_str(), "wb"), &CloseFile);
  if (fp == nullptr) {
    throw std::runtime_error(
        std::string("ONNX export failed. Could not open file or directory: ") +
        fullFilePath);
  }
  fwrite(tensor.data_ptr(), tensor.element_size(), tensor.numel(), fp.get());
} // fclose() called here through CloseFile(), if FILE* is not a null pointer.

class GraphEncoder {
 public:
  GraphEncoder(
      const std::shared_ptr<Graph>& graph,
      int64_t onnx_opset_version,
      onnx_torch::OperatorExportTypes operator_export_type,
      const std::map<std::string, at::Tensor>& initializers,
      const std::unordered_map<
          std::string,
          std::unordered_map<int64_t, std::string>>& dynamic_axes,
      bool defer_weight_export,
      bool strip_doc,
      bool keep_initializers_as_inputs,
      const std::map<std::string, int>& custom_opsets,
      bool add_node_names,
      bool use_external_data_format,
      const std::string& onnx_file_path,
      const ValAttrNameMap& val_attr_to_name = {},
      const NodeAttrNameMap& node_attr_to_name = {});

  onnx::ModelProto get_model_proto() {
    return model_proto_;
  }

  SymbolDimMap get_symbol_dim_param_map() {
    return symbol_dim_map_;
  }

  RawDataExportMap get_raw_data_export_map() {
    return raw_data_export_map_;
  }

  bool get_use_external_data_format() {
    return use_external_data_format_;
  }

 private:
  // Using std::map instead of std::unordered_map for initializers
  // in EncodeGraph constructor so that the order in which initializers
  // get written to the ONNX graph is always the deterministic and
  // predictable. While this is not a ONNX requirement, it is needed
  // for testing purposes in tests that use _export_to_pretty_string()
  // for validating ONNX graphs.
  void EncodeGraph(
      onnx::GraphProto* graph_proto,
      const std::shared_ptr<Graph>& graph,
      const std::map<std::string, at::Tensor>& initializers =
          std::map<std::string, at::Tensor>(),
      const std::
          unordered_map<std::string, std::unordered_map<int64_t, std::string>>&
              dynamic_axes = std::unordered_map<
                  std::string,
                  std::unordered_map<int64_t, std::string>>(),
      bool keep_initializers_as_inputs = true,
      bool add_node_names = true,
      bool use_external_data_format = false,
      const std::string& onnx_file_path = std::string());

  void EncodeBlock(
      onnx::GraphProto* graph_proto,
      const Block* block,
      const std::map<std::string, at::Tensor>& initializers =
          std::map<std::string, at::Tensor>(),
      const std::
          unordered_map<std::string, std::unordered_map<int64_t, std::string>>&
              dynamic_axes = std::unordered_map<
                  std::string,
                  std::unordered_map<int64_t, std::string>>(),
      bool keep_initializers_as_inputs = true,
      bool add_node_names = true,
      bool use_external_data_format = false,
      const std::string& onnx_file_path = std::string());

  void AddInitializersIntoGraphProto(
      onnx::GraphProto* graph_proto,
      const Block* block,
      const std::map<std::string, at::Tensor>& initializers =
          std::map<std::string, at::Tensor>(),
      bool use_external_data_format = false,
      const std::string& onnx_file_path = std::string());

  unsigned long long int GetGraphProtoSize(
      onnx::GraphProto* graph_proto,
      const std::shared_ptr<Graph>& graph,
      const std::map<std::string, at::Tensor>& initializers =
          std::map<std::string, at::Tensor>());

  void EncodeNode(
      onnx::GraphProto* graph_proto,
      onnx::NodeProto* node_proto,
      const Node* node,
      bool add_node_names = true,
      bool use_external_data_format = false,
      const std::string& onnx_file_path = std::string());

  void EncodeLocalFunctionOpsetImport(
      onnx::FunctionProto* func_proto,
      const Node* n,
      std::unordered_set<std::string>& custom_domains);

  void EncodeLocalFunction(
      onnx::GraphProto* graph_proto,
      onnx::FunctionProto* func_proto,
      const Node* n,
      bool add_node_names = true,
      bool use_external_data_format = false,
      const std::string& onnx_file_path = std::string());

  void EncodeTensor(
      onnx::TensorProto* tensor_proto,
      const at::Tensor& tensor,
      const c10::optional<std::string> external_ref = {},
      const bool use_external_data_format = false,
      const std::string& onnx_file_path = std::string());

  void EncodeIntermediateValueInfo(
      onnx::GraphProto* graph_proto,
      const Value* n);

  void EncodeValueInfo(
      onnx::GraphProto* graph_proto,
      onnx::ValueInfoProto* v,
      const Value* n,
      const std::
          unordered_map<std::string, std::unordered_map<int64_t, std::string>>&
              dynamic_axes = std::unordered_map<
                  std::string,
                  std::unordered_map<int64_t, std::string>>());

  void EncodeValueInfoType(
      onnx::TypeProto* onnx_type,
      const TypePtr node_type,
      const Value* n,
      const std::unordered_map<
          std::string,
          std::unordered_map<int64_t, std::string>>& dynamic_axes);

  void AddAttribute(
      onnx::NodeProto* node_proto,
      const jit::Symbol name,
      const std::string& ref_attr_name);

  void AddAttribute(
      onnx::NodeProto* node_proto,
      const jit::Node* node,
      const jit::Symbol name,
      const bool use_external_data_format = false,
      const std::string& onnx_file_path = std::string());

  void AddAttribute(onnx::FunctionProto* func_proto, const std::string& name);

  SymbolDimMap symbol_dim_map_;
  onnx::ModelProto model_proto_;
  size_t num_blocks_;
  size_t num_op_nodes_;
  size_t num_external_data_;
  onnx_torch::OperatorExportTypes operator_export_type_;
  bool strip_doc_;
  std::set<std::string> domains_;
  RawDataExportMap raw_data_export_map_;
  bool defer_weight_export_;
  bool use_external_data_format_;
  int64_t onnx_opset_version_;
  std::map<std::string, int> custom_opsets_;
  std::shared_ptr<Graph> graph_;
  ValAttrNameMap val_attr_to_name_;
  NodeAttrNameMap node_attr_to_name_;
  // For large models, the parameters can be stored in separate binary files.
  // This parameter sets a threshold on the number of elements in the parameter
  // tensor, beyond which the parameter is stored in a separate file (if API
  // argument use_external_data_format is set to True). This threshold is in
  // place so as not to create too many external files.
  const size_t ParamSizeThresholdForExternalStorage = 1024;
};

onnx::TensorProto_DataType ATenTypeToOnnxType(at::ScalarType at_type) {
  switch (at_type) {
    case at::kDouble:
      return onnx::TensorProto_DataType_DOUBLE;
    case at::kFloat:
      return onnx::TensorProto_DataType_FLOAT;
    case at::kHalf:
      return onnx::TensorProto_DataType_FLOAT16;
    case at::kByte:
      return onnx::TensorProto_DataType_UINT8;
    case at::kChar:
      return onnx::TensorProto_DataType_INT8;
    case at::kShort:
      return onnx::TensorProto_DataType_INT16;
    case at::kInt:
      return onnx::TensorProto_DataType_INT32;
    case at::kLong:
      return onnx::TensorProto_DataType_INT64;
    case at::kBool:
      return onnx::TensorProto_DataType_BOOL;
    case at::kQInt8:
      return onnx::TensorProto_DataType_INT8;
    case at::kQUInt8:
      return onnx::TensorProto_DataType_UINT8;
    case at::kQInt32:
      return onnx::TensorProto_DataType_INT32;
    default:
      AT_ERROR("unexpected tensor scalar type");
  }
}

GraphEncoder::GraphEncoder(
    const std::shared_ptr<Graph>& graph,
    int64_t onnx_opset_version,
    onnx_torch::OperatorExportTypes operator_export_type,
    const std::map<std::string, at::Tensor>& initializers,
    const std::unordered_map<
        std::string,
        std::unordered_map<int64_t, std::string>>& dynamic_axes,
    bool defer_weight_export,
    bool strip_doc,
    bool keep_initializers_as_inputs,
    const std::map<std::string, int>& custom_opsets,
    bool add_node_names,
    bool use_external_data_format,
    const std::string& onnx_file_path,
    const ValAttrNameMap& val_attr_to_name,
    const NodeAttrNameMap& node_attr_to_name)
    : num_blocks_(0),
      num_op_nodes_(0),
      num_external_data_(0),
      operator_export_type_(operator_export_type),
      strip_doc_(strip_doc),
      defer_weight_export_(defer_weight_export),
      use_external_data_format_(use_external_data_format),
      onnx_opset_version_(onnx_opset_version),
      custom_opsets_(custom_opsets),
      graph_(graph),
      val_attr_to_name_(val_attr_to_name),
      node_attr_to_name_(node_attr_to_name) {
  model_proto_.set_producer_name("pytorch");
  // we pin IR version to version 6 (12/11/2019) instead of using
  // onnx::IR_VERSION. with this change, the test_operators.py will be more
  // stable. only bump it when it's necessary
  model_proto_.set_ir_version(onnx_torch::IR_VERSION);
  // TODO: set the producer version using appropriate function call
  model_proto_.set_producer_version(onnx_torch::PRODUCER_VERSION);

  validateGraph(graph, operator_export_type);

  // If graph proto size exceed maximum protobuf size of 2GB, set
  // use_external_data_format to true.
  if (!use_external_data_format && !onnx_file_path.empty() &&
      GetGraphProtoSize(model_proto_.mutable_graph(), graph, initializers) >
          INT_MAX) {
    GRAPH_DEBUG(
        "Exporting model exceed maximum protobuf size of 2GB. Storing model parameters in external data files");
    use_external_data_format = true;
    // use_external_data_format_ is one of graph_encoder private variable set
    // for return `use_external_data_format` value.
    use_external_data_format_ = use_external_data_format;
  }

  if (use_external_data_format) {
    TORCH_CHECK(
        !onnx_file_path.empty(),
        "For large model export, f in torch.onnx.export must be a non-empty string "
        "specifying the location of the model.");
  }

  auto* imp = model_proto_.add_opset_import();
  // This is the version of ONNX operator set we are targeting
  imp->set_version(onnx_opset_version);

  EncodeGraph(
      model_proto_.mutable_graph(),
      graph,
      initializers,
      dynamic_axes,
      keep_initializers_as_inputs,
      add_node_names,
      use_external_data_format,
      onnx_file_path);

  for (const std::string& domain : domains_) {
    auto* opset = model_proto_.add_opset_import();
    opset->set_domain(domain);
    //  Check if domain version is registered. If not, set to version 1
    auto it = custom_opsets.find(domain);
    if (it == custom_opsets.end())
      opset->set_version(1);
    else {
      opset->set_version(it->second);
    }
  }

  for (auto const& custom_opset : custom_opsets) {
    if (!std::count(domains_.begin(), domains_.end(), custom_opset.first)) {
      TORCH_WARN(
          "Custom opset domain: '",
          custom_opset.first,
          "' provided is not used in the model. ",
          "Please verify custom opset domain names.");
    }
  }
}

void GraphEncoder::EncodeValueInfoType(
    onnx::TypeProto* onnx_type,
    const TypePtr node_type,
    const Value* n,
    const std::unordered_map<
        std::string,
        std::unordered_map<int64_t, std::string>>& dynamic_axes) {
  auto tensorTypeToONNXType = [&dynamic_axes, n, this](
                                  const TensorTypePtr& t,
                                  onnx::TypeProto_Tensor* onnx_tensor_type) {
    std::string name = n->debugName();
    if (t->dim()) {
      onnx::TensorShapeProto* shape = onnx_tensor_type->mutable_shape();
      auto sizes = t->symbolic_sizes().sizes().value();
      for (const auto i : c10::irange(sizes.size())) {
        shape->add_dim();
        if ((dynamic_axes.find(name) != dynamic_axes.end()) &&
            (dynamic_axes.at(name).find(i) != dynamic_axes.at(name).end())) {
          shape->mutable_dim(i)->set_dim_param(dynamic_axes.at(name).at(i));
          if (!sizes[i].is_static()) {
            symbol_dim_map_[sizes[i]] = dynamic_axes.at(name).at(i);
          }
        } else if (sizes[i].is_static()) {
          shape->mutable_dim(i)->set_dim_value(sizes[i].static_size());
        } else {
          if (symbol_dim_map_.find(sizes[i]) == symbol_dim_map_.end()) {
            if (n->node()->kind() == prim::Param) {
              symbol_dim_map_[sizes[i]] = name + "_dim_" + std::to_string(i);
            } else {
              std::string op_type = n->node()->kind().toUnqualString();
              symbol_dim_map_[sizes[i]] =
                  op_type + name + "_dim_" + std::to_string(i);
            }
          }
          shape->mutable_dim(i)->set_dim_param(symbol_dim_map_[sizes[i]]);
        }
      }
    }
    if (t->scalarType()) {
      onnx_tensor_type->set_elem_type(
          ATenTypeToOnnxType(t->scalarType().value()));
    }
  };

  if (TensorTypePtr tensor_type = node_type->cast<TensorType>()) {
    if (tensor_type->dim() || tensor_type->scalarType()) {
      // Encode type if either shape or dtype exists.
      onnx::TypeProto_Tensor* onnx_tensor_type =
          onnx_type->mutable_tensor_type();
      tensorTypeToONNXType(tensor_type, onnx_tensor_type);
    }
  } else if (BoolTypePtr bool_type = node_type->cast<BoolType>()) {
    onnx::TypeProto_Tensor* onnx_tensor_type = onnx_type->mutable_tensor_type();
    onnx_tensor_type->set_elem_type(ATenTypeToOnnxType(at::kBool));
  } else if (IntTypePtr int_type = node_type->cast<IntType>()) {
    onnx::TypeProto_Tensor* onnx_tensor_type = onnx_type->mutable_tensor_type();
    onnx_tensor_type->set_elem_type(ATenTypeToOnnxType(at::kLong));
  } else if (FloatTypePtr float_type = node_type->cast<FloatType>()) {
    onnx::TypeProto_Tensor* onnx_tensor_type = onnx_type->mutable_tensor_type();
    onnx_tensor_type->set_elem_type(ATenTypeToOnnxType(at::kFloat));
  } else if (ListTypePtr list_type = node_type->cast<ListType>()) {
    auto list_elem_type = list_type->getElementType();
    onnx::TypeProto_Sequence* sequence_type =
        onnx_type->mutable_sequence_type();
    onnx::TypeProto* onnx_tensor_type = sequence_type->mutable_elem_type();
    EncodeValueInfoType(onnx_tensor_type, list_elem_type, n, dynamic_axes);
  }
}

void GraphEncoder::EncodeValueInfo(
    onnx::GraphProto* graph_proto,
    onnx::ValueInfoProto* v,
    const Value* n,
    const std::unordered_map<
        std::string,
        std::unordered_map<int64_t, std::string>>& dynamic_axes) {
  std::string name = n->debugName();
  v->set_name(name);
  EncodeValueInfoType(v->mutable_type(), n->type(), n, dynamic_axes);
}

void GraphEncoder::EncodeGraph(
    onnx::GraphProto* graph_proto,
    const std::shared_ptr<Graph>& graph,
    const std::map<std::string, at::Tensor>& initializers,
    const std::unordered_map<
        std::string,
        std::unordered_map<int64_t, std::string>>& dynamic_axes,
    bool keep_initializers_as_inputs,
    bool add_node_names,
    bool use_external_data_format,
    const std::string& onnx_file_path) {
  EncodeBlock(
      graph_proto,
      graph->block(),
      initializers,
      dynamic_axes,
      keep_initializers_as_inputs,
      add_node_names,
      use_external_data_format,
      onnx_file_path);
}

void GraphEncoder::EncodeBlock(
    onnx::GraphProto* graph_proto,
    const Block* block,
    const std::map<std::string, at::Tensor>& initializers,
    const std::unordered_map<
        std::string,
        std::unordered_map<int64_t, std::string>>& dynamic_axes,
    bool keep_initializers_as_inputs,
    bool add_node_names,
    bool use_external_data_format,
    const std::string& onnx_file_path) {
  AT_ASSERT(graph_proto != nullptr);
  std::string block_name = "torch-jit-export";
  if (num_blocks_) {
    block_name += std::to_string(num_blocks_);
  }
  num_blocks_++;
  graph_proto->set_name(block_name);

  // Since ONNX IR VERSION 4, initializers do not have to
  // be a subset of graph inputs. We use keep_initializers_as_inputs
  // argument to determine whether to add initializers
  // as inputs or not. If keep_initializers_as_inputs=false,
  // we only add non-parameter inputs as inputs to ONNX graph, and
  // not the initializers (parameters). If keep_initializers_as_inputs
  // =true, we add initializers as inputs too. Setting
  // keep_initializers_as_inputs=false allows better
  // optimizations, such as constant-folding, on ONNX graphs
  // by backends/optimizers.
  if (keep_initializers_as_inputs) {
    for (auto input : block->inputs()) {
      onnx::ValueInfoProto* v = graph_proto->add_input();
      EncodeValueInfo(graph_proto, v, input, dynamic_axes);
    }
  } else {
    for (auto input : block->inputs()) {
      auto it = initializers.find(input->debugName());
      if (it == initializers.end()) {
        onnx::ValueInfoProto* v = graph_proto->add_input();
        EncodeValueInfo(graph_proto, v, input, dynamic_axes);
      }
    }
  }
  for (auto output : block->outputs()) {
    onnx::ValueInfoProto* v = graph_proto->add_output();
    EncodeValueInfo(graph_proto, v, output, dynamic_axes);
  }
  for (auto node : block->nodes()) {
    if (node->mustBeNone()) {
      // None nodes are used to implement optional inputs. One
      // way to "not provide" an optional input is to create an
      // Undefined node, and pass its output as that input.
      continue;
    }
    if (node->kind() == ::c10::Symbol::onnx("LocalFunctionDef")) {
      auto* func_proto = model_proto_.add_functions();
      EncodeLocalFunction(
          graph_proto,
          func_proto,
          node,
          add_node_names,
          use_external_data_format,
          onnx_file_path);
      continue;
    }
    auto* n_proto = graph_proto->add_node();
    EncodeNode(
        graph_proto,
        n_proto,
        node,
        add_node_names,
        use_external_data_format,
        onnx_file_path);
  }
  AddInitializersIntoGraphProto(
      graph_proto,
      block,
      initializers,
      use_external_data_format,
      onnx_file_path);
}

void GraphEncoder::AddInitializersIntoGraphProto(
    onnx::GraphProto* graph_proto,
    const Block* block,
    const std::map<std::string, at::Tensor>& initializers,
    bool use_external_data_format,
    const std::string& onnx_file_path) {
  AT_ASSERT(block->inputs().size() >= initializers.size());
  for (auto input : block->inputs()) {
    auto name_tensor_pair = initializers.find(input->debugName());
    if (name_tensor_pair == initializers.end()) {
      continue;
    }
    auto p = graph_proto->add_initializer();
    p->set_name(name_tensor_pair->first);
    EncodeTensor(
        p,
        name_tensor_pair->second,
        name_tensor_pair->first,
        use_external_data_format,
        onnx_file_path);
  }
}

unsigned long long int GraphEncoder::GetGraphProtoSize(
    onnx::GraphProto* graph_proto,
    const std::shared_ptr<Graph>& graph,
    const std::map<std::string, at::Tensor>& initializers) {
  unsigned long long int sizes = 0;
  for (auto input : graph->inputs()) {
    auto name_tensor_pair = initializers.find(input->debugName());
    if (name_tensor_pair == initializers.end()) {
      continue;
    }
    onnx::GraphProto* graph_proto_copy = new onnx::GraphProto(*graph_proto);
    auto tensor_proto = graph_proto_copy->add_initializer();
    const at::Tensor tensor = name_tensor_pair->second;
    for (auto d : tensor.sizes()) {
      tensor_proto->add_dims(d);
    }
    tensor_proto->set_data_type(ATenTypeToOnnxType(tensor.scalar_type()));
    at::Tensor t;
    if (tensor.is_quantized()) {
      t = tensor.contiguous();
    } else {
      t = tensor.contiguous().cpu();
    }
    tensor_proto->set_raw_data(std::string(
        static_cast<char*>(t.data_ptr()), t.element_size() * t.numel()));
    sizes += tensor_proto->ByteSizeLong();
    delete graph_proto_copy;
    graph_proto_copy = nullptr;
  }
  return sizes;
}

void GraphEncoder::EncodeNode(
    onnx::GraphProto* graph_proto,
    onnx::NodeProto* node_proto,
    const Node* node,
    bool add_node_names,
    bool use_external_data_format,
    const std::string& onnx_file_path) {
  if (!strip_doc_) {
    node_proto->set_doc_string(node->sourceRange().str());
  }
  for (auto input : node->inputs()) {
    if (input->node()->mustBeNone()) {
      node_proto->add_input("");
    } else {
      auto attr_it = val_attr_to_name_.find(input);
      if (attr_it != val_attr_to_name_.end()) {
        node_proto->add_input(attr_it->second);
      } else {
        node_proto->add_input(input->debugName());
      }
    }
  }
  for (auto output : node->outputs()) {
    node_proto->add_output(output->debugName());
    EncodeIntermediateValueInfo(graph_proto, output);
  }
  if (!node->kind().is_onnx()) {
    std::string domain;
    if (node->kind().is_aten() || node->kind().is_caffe2()) {
      domain = node->kind().domainString();
    } else { //  Custom namespace and domain
      domain = node->kind().ns().toUnqualString();
    }
    // TODO: set correct domain for function proto.
    domains_.insert(domain);
    node_proto->set_domain(domain);
  }
  if (operator_export_type_ == onnx_torch::OperatorExportTypes::ONNX) {
    AT_ASSERT(
        !node->kind().is_aten() && !node->kind().is_prim() &&
        !node->kind().is_attr());
  }
  node_proto->set_op_type(node->kind().toUnqualString());
  if (add_node_names) {
    node_proto->set_name(
        node_proto->op_type() + "_" + std::to_string(num_op_nodes_));
    num_op_nodes_++;
  }
  auto attrs_it = node_attr_to_name_.find(node);
  for (auto attr_name : node->attributeNames()) {
    if (attrs_it != node_attr_to_name_.end()) {
      auto attr_it = attrs_it->second.find(attr_name.toUnqualString());
      if (attr_it != attrs_it->second.end()) {
        AddAttribute(node_proto, attr_name, attr_it->second);
        continue;
      }
    }
    AddAttribute(
        node_proto, node, attr_name, use_external_data_format, onnx_file_path);
  }
  if (node->kind() == ::c10::onnx::Loop) {
    AT_ASSERT(node->blocks().size() == 1);

    auto body = node_proto->add_attribute();
    body->set_name("body");
    body->set_type(onnx::AttributeProto_AttributeType_GRAPH);
    auto g = body->mutable_g();
    EncodeBlock(
        g,
        node->blocks()[0],
        {},
        {},
        true,
        true,
        use_external_data_format,
        onnx_file_path);
  }
  if (node->kind() == ::c10::onnx::If) {
    AT_ASSERT(node->blocks().size() == 2);

    auto then_branch = node_proto->add_attribute();
    then_branch->set_name("then_branch");
    then_branch->set_type(onnx::AttributeProto_AttributeType_GRAPH);
    auto true_g = then_branch->mutable_g();
    EncodeBlock(
        true_g,
        node->blocks()[0],
        {},
        {},
        true,
        true,
        use_external_data_format,
        onnx_file_path);

    auto else_branch = node_proto->add_attribute();
    else_branch->set_name("else_branch");
    else_branch->set_type(onnx::AttributeProto_AttributeType_GRAPH);
    auto false_g = else_branch->mutable_g();
    EncodeBlock(
        false_g,
        node->blocks()[1],
        {},
        {},
        true,
        true,
        use_external_data_format,
        onnx_file_path);
  }
}

void GraphEncoder::AddAttribute(
    onnx::NodeProto* node_proto,
    const jit::Symbol name,
    const std::string& ref_attr_name) {
  auto attr = node_proto->add_attribute();
  AT_ASSERT(name.is_attr());
  attr->set_name(name.toUnqualString());
  attr->set_ref_attr_name(ref_attr_name);
}

void GraphEncoder::AddAttribute(
    onnx::NodeProto* node_proto,
    const jit::Node* node,
    const jit::Symbol name,
    const bool use_external_data_format,
    const std::string& onnx_file_path) {
  auto createAttributeTensorName =
      [](const onnx::NodeProto* node_proto,
         onnx::TensorProto* tensor_proto,
         const jit::Symbol attr_name,
         size_t& num_external_data) -> std::string {
    if (tensor_proto->has_name()) {
      return tensor_proto->name();
    }
    if (!node_proto->has_name()) {
      auto name = node_proto->op_type() + "_" + attr_name.toDisplayString() +
          "_" + std::to_string(num_external_data);
      num_external_data++;
      return name;
    } else {
      return node_proto->name() + "_" + attr_name.toDisplayString();
    }
  };

  auto attr = node_proto->add_attribute();
  AT_ASSERT(name.is_attr());
  attr->set_name(name.toUnqualString());
  switch (node->kindOf(name)) {
    case AttributeKind::f:
      attr->set_f(node->f(name));
      attr->set_type(onnx::AttributeProto_AttributeType_FLOAT);
      break;
    case AttributeKind::fs:
      attr->set_type(onnx::AttributeProto_AttributeType_FLOATS);
      for (auto& v : node->fs(name))
        // NOLINTNEXTLINE(bugprone-narrowing-conversions,cppcoreguidelines-narrowing-conversions)
        attr->add_floats(v);
      break;
    case AttributeKind::i:
      attr->set_type(onnx::AttributeProto_AttributeType_INT);
      attr->set_i(node->i(name));
      break;
    case AttributeKind::is:
      attr->set_type(onnx::AttributeProto_AttributeType_INTS);
      for (auto& v : node->is(name))
        attr->add_ints(v);
      break;
    case AttributeKind::s:
      attr->set_type(onnx::AttributeProto_AttributeType_STRING);
      attr->set_s(node->s(name));
      break;
    case AttributeKind::ss:
      attr->set_type(onnx::AttributeProto_AttributeType_STRINGS);
      for (auto& v : node->ss(name))
        attr->add_strings(v);
      break;
    case AttributeKind::t: {
      attr->set_type(onnx::AttributeProto_AttributeType_TENSOR);
      auto t = attr->mutable_t();
      if (use_external_data_format && !t->has_name()) {
        t->set_name(
            createAttributeTensorName(node_proto, t, name, num_external_data_));
      }
      EncodeTensor(
          t, node->t(name), {}, use_external_data_format, onnx_file_path);
    } break;
    case AttributeKind::ts:
      attr->set_type(onnx::AttributeProto_AttributeType_TENSORS);
      for (auto& v : node->ts(name)) {
        auto t = attr->add_tensors();
        if (use_external_data_format && !t->has_name()) {
          t->set_name(createAttributeTensorName(
              node_proto, t, name, num_external_data_));
        }
        EncodeTensor(t, v, {}, use_external_data_format, onnx_file_path);
      }
      break;
    case AttributeKind::g: {
      attr->set_type(onnx::AttributeProto_AttributeType_GRAPH);
      auto g = attr->mutable_g();
      EncodeGraph(
          g,
          node->g(name),
          {},
          {},
          true,
          true,
          use_external_data_format,
          onnx_file_path);
    } break;
    case AttributeKind::gs:
      attr->set_type(onnx::AttributeProto_AttributeType_GRAPHS);
      for (auto& v : node->gs(name)) {
        auto g = attr->add_graphs();
        EncodeGraph(
            g, v, {}, {}, true, true, use_external_data_format, onnx_file_path);
      }
      break;
    default:
      std::ostringstream err_msg;
      err_msg << "attribute \"" << name.toDisplayString()
              << "\" has unexpected kind: " << toString(node->kindOf(name));
      throw std::runtime_error(err_msg.str());
  }
}

void GraphEncoder::AddAttribute(
    onnx::FunctionProto* func_proto,
    const std::string& name) {
  TORCH_INTERNAL_ASSERT(nullptr != func_proto);
  func_proto->add_attribute(name);
}

void GraphEncoder::EncodeLocalFunctionOpsetImport(
    onnx::FunctionProto* func_proto,
    const Node* n,
    std::unordered_set<std::string>& custom_domains) {
  if (!n->kind().is_onnx()) {
    std::string domain;
    if (n->kind().is_aten() || n->kind().is_caffe2()) {
      domain = n->kind().domainString();
    } else { //  Custom namespace and domain
      domain = n->kind().ns().toUnqualString();
    }
    domains_.insert(domain);

    if (custom_domains.find(domain) == custom_domains.end()) {
      custom_domains.insert(domain);

      auto* custom_imp = func_proto->add_opset_import();
      custom_imp->set_domain(domain);
      //  Check if domain version is registered. If not, set to version 1
      auto it = custom_opsets_.find(domain);
      if (it == custom_opsets_.end())
        custom_imp->set_version(1);
      else {
        custom_imp->set_version(it->second);
      }
    }
  }

  for (auto* b : n->blocks()) {
    for (auto* sub_n : b->nodes()) {
      EncodeLocalFunctionOpsetImport(func_proto, sub_n, custom_domains);
    }
  }
}

void GraphEncoder::EncodeLocalFunction(
    onnx::GraphProto* graph_proto,
    onnx::FunctionProto* func_proto,
    const Node* n,
    bool add_node_names,
    bool use_external_data_format,
    const std::string& onnx_file_path) {
  const auto fsub_g = n->g(Symbol::attr("graph"));
  func_proto->set_name(n->s(::c10::attr::name));

  for (auto input : fsub_g->inputs()) {
    func_proto->add_input(input->debugName());
  }
  for (auto output : fsub_g->outputs()) {
    func_proto->add_output(output->debugName());
  }

  // encode attributes names
  if (n->hasAttribute(Symbol::attr("attributes"))) {
    for (auto attr_name : n->ss(Symbol::attr("attributes"))) {
      AddAttribute(func_proto, attr_name);
    }
  }

  auto* imp = func_proto->add_opset_import();
  // This is the version of ONNX operator set we are targeting
  imp->set_version(onnx_opset_version_);

  // add for custom domain as well.
  const auto& domain = n->s(Symbol::attr("domain"));
  func_proto->set_domain(domain);
  domains_.insert(domain);
  std::unordered_set<std::string> custom_domains;

  for (auto* fsub_n : fsub_g->nodes()) {
    if (fsub_n->mustBeNone()) {
      // None nodes are used to implement optional inputs. One
      // way to "not provide" an optional input is to create an
      // Undefined node, and pass its output as that input.
      continue;
    }
    auto* n_proto = func_proto->add_node();
    EncodeNode(
        graph_proto,
        n_proto,
        fsub_n,
        add_node_names,
        use_external_data_format,
        onnx_file_path);
    EncodeLocalFunctionOpsetImport(func_proto, fsub_n, custom_domains);
  }
}

void GraphEncoder::EncodeTensor(
    onnx::TensorProto* tensor_proto,
    const at::Tensor& tensor,
    const c10::optional<std::string> external_ref,
    const bool use_external_data_format,
    const std::string& onnx_file_path) {
  for (auto d : tensor.sizes()) {
    tensor_proto->add_dims(d);
  }
  tensor_proto->set_data_type(ATenTypeToOnnxType(tensor.scalar_type()));
  at::Tensor t;
  // CPU's HalfTensor doesn't have contiguous(), so first calling contiguous()
  // TODO We don't call .cpu() on quantized tensors as it fails when calling
  // aten::empty() on quantized tensors beyond certain size. Issue #29435.
  if (tensor.is_quantized()) {
    t = tensor.contiguous();
  } else {
    t = tensor.contiguous().cpu();
  }

  // Either defer_weight_export should be true and external_ref must be present,
  // or use_external_data_format should be true, not both at the same time. They
  // can both be false at the same time (for ONNX export for regular model
  // size).
  AT_ASSERT(
      !((defer_weight_export_ && external_ref) && use_external_data_format));
  // Add a buffer to the raw_data_export_map for the caller to dump into an
  // external data store. If external_ref is not specified, we instead dump
  // the contiguous data into the protobuf itself
  if (defer_weight_export_ && external_ref) {
    // For now, we use the name of the tensor as the external lookup name to
    // avoid ONNX protobuf changes.
    AT_ASSERT(external_ref.value() == tensor_proto->name());
    AT_ASSERT(raw_data_export_map_.count(external_ref.value()) == 0);
    raw_data_export_map_[external_ref.value()] = t;
    tensor_proto->set_raw_data("__EXTERNAL");
  } else {
    AT_ASSERT(t.is_contiguous());
    size_t tensorSize = static_cast<size_t>(c10::multiply_integers(
        std::begin(tensor.sizes()), std::end(tensor.sizes())));
    if (use_external_data_format &&
        tensorSize > ParamSizeThresholdForExternalStorage) {
      AT_ASSERT(!onnx_file_path.empty());
      AT_ASSERT(tensor_proto->has_name());
      auto tensorName = GetExternalFileName(tensor_proto->name());
      CreateExternalFile(t, tensorName, onnx_file_path);
      onnx::StringStringEntryProto* location =
          tensor_proto->mutable_external_data()->Add();
      location->set_key("location");
      location->set_value(tensorName);
      tensor_proto->set_data_location(onnx::TensorProto_DataLocation_EXTERNAL);
    } else {
      tensor_proto->set_raw_data(std::string(
          static_cast<char*>(t.data_ptr()), t.element_size() * t.numel()));
    }
  }
}

void GraphEncoder::EncodeIntermediateValueInfo(
    onnx::GraphProto* graph_proto,
    const Value* v) {
  // Motivation is to encode ValueInfo for onnx local function nodes.
  auto n = v->node();
  if (n->kind().is_onnx()) {
    // Encode value info only for non-onnx nodes.
    return;
  }
  if (n->owningGraph() != graph_.get()) {
    // Encode value info only for node in main graph.
    return;
  }
  for (const auto* o : graph_->outputs()) {
    // Do not encode value info for graph outputs.
    if (o == v) {
      return;
    }
  }
  auto v_info_p = graph_proto->add_value_info();
  EncodeValueInfo(graph_proto, v_info_p, v);
}

} // namespace

std::string pretty_print_onnx(
    const std::shared_ptr<Graph>& graph,
    const std::map<std::string, at::Tensor>& initializers,
    int64_t onnx_opset_version,
    bool defer_weight_export,
    ::torch::onnx::OperatorExportTypes operator_export_type,
    bool google_printer,
    bool keep_initializers_as_inputs,
    const std::map<std::string, int>& custom_opsets,
    bool add_node_names) {
  auto graph_encoder = GraphEncoder(
      graph,
      onnx_opset_version,
      operator_export_type,
      initializers,
      std::unordered_map<
          std::string,
          std::unordered_map<int64_t, std::string>>{},
      defer_weight_export,
      true,
      keep_initializers_as_inputs,
      custom_opsets,
      add_node_names,
      false,
      std::string());
  if (google_printer) {
    return graph_encoder.get_model_proto().DebugString();
  }
  return prettyPrint(graph_encoder.get_model_proto());
}

std::tuple<
    std::shared_ptr<::ONNX_NAMESPACE::ModelProto>,
    RawDataExportMap,
    SymbolDimMap,
    bool>
export_onnx(
    const std::shared_ptr<Graph>& graph,
    const std::map<std::string, at::Tensor>& initializers,
    int64_t onnx_opset_version,
    const std::unordered_map<
        std::string,
        std::unordered_map<std::int64_t, std::string>>& dynamic_axes,
    bool defer_weight_export,
    ::torch::onnx::OperatorExportTypes operator_export_type,
    bool strip_doc_string,
    bool keep_initializers_as_inputs,
    const std::map<std::string, int>& custom_opsets,
    bool add_node_names,
    bool use_external_data_format,
    const std::string& onnx_file_path,
    const ValAttrNameMap& val_attr_to_name,
    const NodeAttrNameMap& node_attr_to_name) {
  auto graph_encoder = GraphEncoder(
      graph,
      onnx_opset_version,
      operator_export_type,
      initializers,
      dynamic_axes,
      defer_weight_export,
      strip_doc_string,
      keep_initializers_as_inputs,
      custom_opsets,
      add_node_names,
      use_external_data_format,
      onnx_file_path,
      // module_names_to_function,
      val_attr_to_name,
      node_attr_to_name);
  GRAPH_DEBUG("onnx proto:", prettyPrint(graph_encoder.get_model_proto()));
  return std::make_tuple(
      std::make_shared<::ONNX_NAMESPACE::ModelProto>(
          graph_encoder.get_model_proto()),
      graph_encoder.get_raw_data_export_map(),
      graph_encoder.get_symbol_dim_param_map(),
      graph_encoder.get_use_external_data_format());
}

std::string serialize_model_proto_to_string(
    const std::shared_ptr<::ONNX_NAMESPACE::ModelProto>& model_proto) {
  const size_t proto_size = model_proto->ByteSizeLong();
  TORCH_CHECK(
      proto_size <= INT_MAX,
      "Exporting model exceed maximum protobuf size of 2GB. "
      "Please call torch.onnx.export without setting use_external_data_format parameter.");
  return model_proto->SerializeAsString();
}

void check_onnx_proto(const std::string& proto_string) {
  onnx::ModelProto model;
  if (!ParseProtoFromBytes(&model, proto_string.c_str(), proto_string.size())) {
    throw std::runtime_error("Invalid ONNX proto string.");
    return;
  }
  onnx::checker::check_model(model);
}

} // namespace jit
} // namespace torch
