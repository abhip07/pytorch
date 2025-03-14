#include <torch/csrc/jit/jit_log.h>
#include <torch/csrc/jit/passes/onnx/helper.h>

#include <onnx/onnx_pb.h>

namespace torch {
namespace jit {
namespace onnx {
using namespace ::c10::onnx;

} // namespace onnx

ValueToParamPairMap buildValueToParamsMap(
    Block* b,
    const ParamMap& paramsDict) {
  ValueToParamPairMap valsToParamsMap;
  for (auto& input : b->inputs()) {
    auto it = paramsDict.find(input->debugName());
    if (it != paramsDict.end()) {
      valsToParamsMap.emplace(input, *it);
    }
  }
  return valsToParamsMap;
}

void eraseUnusedBlockInputs(Block* b) {
  for (size_t i_1 = b->inputs().size(); i_1 > 0; --i_1) {
    size_t i = i_1 - 1;
    if (!b->inputs().at(i)->hasUses()) {
      b->eraseInput(i);
    }
  }
}

void eraseUnusedValuesFromMap(ValueToParamPairMap& valsToParamsMap) {
  auto it = valsToParamsMap.begin();
  while (it != valsToParamsMap.end()) {
    if (!it->first->hasUses()) {
      it = valsToParamsMap.erase(it);
    } else {
      ++it;
    }
  }
}

void buildParamsMapFromValueToParamsMap(
    const ValueToParamPairMap& valsToParamsMap,
    ParamMap& paramsDict) {
  paramsDict.clear();
  for (const auto& nameTensorParamPair : valsToParamsMap) {
    paramsDict.insert(nameTensorParamPair.second);
  }
}

c10::optional<at::ScalarType> ONNXTypeToATenType(int32_t onnx_type) {
  switch (onnx_type) {
    case ::ONNX_NAMESPACE::TensorProto_DataType_UNDEFINED:
      return at::ScalarType::Undefined;
    case ::ONNX_NAMESPACE::TensorProto_DataType_FLOAT:
      return at::kFloat;
    case ::ONNX_NAMESPACE::TensorProto_DataType_UINT8:
      return at::kByte;
    case ::ONNX_NAMESPACE::TensorProto_DataType_INT8:
      return at::kChar;
    case ::ONNX_NAMESPACE::TensorProto_DataType_INT16:
      return at::kShort;
    case ::ONNX_NAMESPACE::TensorProto_DataType_INT32:
      return at::kInt;
    case ::ONNX_NAMESPACE::TensorProto_DataType_INT64:
      return at::kLong;
    case ::ONNX_NAMESPACE::TensorProto_DataType_BOOL:
      return at::kBool;
    case ::ONNX_NAMESPACE::TensorProto_DataType_FLOAT16:
      return at::kHalf;
    case ::ONNX_NAMESPACE::TensorProto_DataType_DOUBLE:
      return at::kDouble;
    case ::ONNX_NAMESPACE::TensorProto_DataType_COMPLEX64:
      return at::kComplexFloat;
    case ::ONNX_NAMESPACE::TensorProto_DataType_COMPLEX128:
      return at::kComplexDouble;
    case ::ONNX_NAMESPACE::TensorProto_DataType_BFLOAT16:
      return at::kBFloat16;
    default:
      TORCH_CHECK(false, "unexpected tensor scalar type");
  }
  return c10::optional<at::ScalarType>{};
}

Node* addNodeToBlock(Block* block, Symbol kind, ArrayRef<Value*> inputs) {
  auto new_node = block->appendNode(block->owningGraph()->create(kind));
  for (auto input : inputs) {
    // NOLINTNEXTLINE(clang-analyzer-deadcode.DeadStores)
    auto new_input = new_node->addInput(input);
  }
  return new_node;
}

Value* addInputToBlock(Block* block) {
  return block->addInput();
}

namespace {
::ONNX_NAMESPACE::TensorProto_DataType ATenTypeToOnnxType_aux(
    at::ScalarType at_type) {
  switch (at_type) {
    case at::kDouble:
      return ::ONNX_NAMESPACE::TensorProto_DataType_DOUBLE;
    case at::kFloat:
      return ::ONNX_NAMESPACE::TensorProto_DataType_FLOAT;
    case at::kHalf:
      return ::ONNX_NAMESPACE::TensorProto_DataType_FLOAT16;
    case at::kByte:
      return ::ONNX_NAMESPACE::TensorProto_DataType_UINT8;
    case at::kChar:
      return ::ONNX_NAMESPACE::TensorProto_DataType_INT8;
    case at::kShort:
      return ::ONNX_NAMESPACE::TensorProto_DataType_INT16;
    case at::kInt:
      return ::ONNX_NAMESPACE::TensorProto_DataType_INT32;
    case at::kLong:
      return ::ONNX_NAMESPACE::TensorProto_DataType_INT64;
    case at::kBool:
      return ::ONNX_NAMESPACE::TensorProto_DataType_BOOL;
    case at::kQInt8:
      return ::ONNX_NAMESPACE::TensorProto_DataType_INT8;
    case at::kQUInt8:
      return ::ONNX_NAMESPACE::TensorProto_DataType_UINT8;
    case at::kQInt32:
      return ::ONNX_NAMESPACE::TensorProto_DataType_INT32;
    default:
      AT_ERROR("unexpected tensor scalar type");
  }
}
} // namespace

int ATenTypeToOnnxType(at::ScalarType at_type) {
  return static_cast<int>(ATenTypeToOnnxType_aux(at_type));
}

Node* createONNXUnsqueeze(
    Graph* graph,
    Node* n_to_insert_before,
    Value* input,
    int axis,
    int opset_version) {
  Node* unsqueeze_node = graph->create(onnx::Unsqueeze, 1);
  unsqueeze_node->addInput(input);
  unsqueeze_node->insertBefore(n_to_insert_before);
  if (opset_version >= OPSET_VERSION_13) {
    // ONNX spec sets `axes` as input for opset >= 13.
    Node* unsqueeze_axes = graph->create(onnx::Constant, 1);
    unsqueeze_axes->insertBefore(unsqueeze_node);
    unsqueeze_axes->t_(
        attr::value, at::unsqueeze(at::scalar_to_tensor(at::Scalar(axis)), 0));
    unsqueeze_node->addInput(unsqueeze_axes->output());
  } else {
    // ONNX spec sets `axes` as attribute for opset < 13.
    unsqueeze_node->is_(attr::axes, {0});
  }
  return unsqueeze_node;
}

bool isValidToTransformToONNXConcatNode(Node* lc_node) {
  return !lc_node->inputs().empty();
}

Node* transformToONNXConcatNode(
    Graph* g,
    Node* lc_node,
    bool need_new_input,
    int opset_version) {
  // ListConstruct Int[] output case, we need to transform to ONNX
  // Concat to ensure the output is a single tensor(dynamic) type in
  // order to be consumed as inputs
  std::vector<Value*> unsqueezed;
  auto new_node = need_new_input ? g->return_node() : lc_node;

  for (auto* input : lc_node->inputs()) {
    auto new_input =
        need_new_input ? g->addInput()->copyMetadata(input) : input;

    Node* unsqueezed_node =
        createONNXUnsqueeze(g, new_node, new_input, 0, opset_version);
    unsqueezed.emplace_back(unsqueezed_node->output());
  }

  Node* concat_node = need_new_input
      ? g->insertNode(g->create(onnx::Concat, 1))
      : g->create(onnx::Concat, 1)->insertBefore(lc_node);
  concat_node->i_(attr::axis, 0);
  for (auto v : unsqueezed) {
    concat_node->addInput(v);
  }

  return concat_node;
}

void ONNXLintGraph(
    const Block* b,
    std::vector<NodeKind>& n_miss_sourceRange,
    std::vector<NodeKind>& n_miss_scope) {
  for (const auto* n : b->nodes()) {
    for (const auto* sub_b : n->blocks()) {
      ONNXLintGraph(sub_b, n_miss_sourceRange, n_miss_scope);
    }

    if (nullptr == n->sourceRange().source()) {
      GRAPH_DEBUG("Node does not set sourceRange:", *n);
      n_miss_sourceRange.emplace_back(n->kind());
    }
    if (n->scopeName() == "") {
      GRAPH_DEBUG("Node does not set scope:", *n);
      n_miss_scope.emplace_back(n->kind());
    }
  }
}

void ONNXLintGraph(const std::shared_ptr<Graph>& graph) {
  // 1. Print nodes that does not have scope/source range covered.
  std::vector<NodeKind> n_miss_sourceRange, n_miss_scope;
  ONNXLintGraph(graph->block(), n_miss_sourceRange, n_miss_scope);
  auto count_const = [](const std::vector<NodeKind>& vec) -> size_t {
    size_t count = 0;
    for (auto k : vec) {
      switch (k) {
        case prim::Constant:
        case prim::ListConstruct:
        case onnx::Constant:
          count++;
          break;
      }
    }
    return count;
  };
  auto const_count_src = count_const(n_miss_sourceRange);
  auto const_count_scope = count_const(n_miss_scope);
  GRAPH_UPDATE("Missing sourceRange.")
  GRAPH_UPDATE(
      "Total ",
      n_miss_sourceRange.size(),
      " nodes. Including ",
      const_count_src,
      " constants.");
  GRAPH_UPDATE("Missing scope.")
  GRAPH_UPDATE(
      "Total ",
      n_miss_scope.size(),
      " nodes. Including ",
      const_count_scope,
      " constants.");
}

} // namespace jit
} // namespace torch
