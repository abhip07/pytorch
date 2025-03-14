# Owner(s): ["oncall: fx"]

import math
import numbers
import operator
import sys
import unittest
from typing import Callable, Dict, Union, List, Optional

import torch
import torch.fx.experimental.optimization as optimization
from torch.fx._symbolic_trace import symbolic_trace
from torch.fx.experimental import merge_matmul
from torch.fx.experimental.accelerator_partitioner import Partitioner
from torch.fx.experimental.normalize import NormalizeOperators, NormalizeArgs
from torch.fx.passes import graph_manipulation
from torch.fx.passes.param_fetch import lift_lowering_attrs_to_nodes
from torch.fx.experimental.partitioner_utils import (
    NodeLatency,
    get_partition_to_latency_mapping,
    get_latency_of_partitioned_graph,
    Device,
    PartitionerConfig,
    PartitionMode,
)
from torch.fx.experimental.rewriter import RewritingTracer
from torch.fx.experimental.schema_type_annotation import AnnotateTypesWithSchema
from torch.fx.graph_module import GraphModule
from torch.fx.node import Node
from torch.fx.operator_schemas import (
    _torchscript_type_to_python_type,
    normalize_function,
    normalize_module,
    type_matches,
    create_type_hint,
)
from torch.fx.passes.shape_prop import _extract_tensor_metadata, ShapeProp
from torch.fx.passes.split_module import split_module
from torch.testing._internal.common_device_type import (
    ops,
    onlyCPU,
    instantiate_device_type_tests,
)
from torch.testing._internal.common_methods_invocations import op_db
from torch.testing._internal.common_nn import module_tests, new_module_tests
from torch.testing._internal.common_utils import run_tests
from torch.testing._internal.jit_utils import JitTestCase

try:
    import torchvision.models
    from torchvision.models import resnet18

    HAS_TORCHVISION = True
except ImportError:
    HAS_TORCHVISION = False
skipIfNoTorchVision = unittest.skipIf(not HAS_TORCHVISION, "no torchvision")
skipIfNoMkldnn = unittest.skipIf(
    not (torch.backends.mkldnn.enabled and torch.backends.mkldnn.is_available()),
    "no MKLDNN",
)


def symbolic_trace_with_rewrite(root: Union[torch.nn.Module, Callable]) -> GraphModule:
    return GraphModule(
        root if isinstance(root, torch.nn.Module) else torch.nn.Module(),
        RewritingTracer().trace(root),
    )


class TestFXExperimental(JitTestCase):
    def test_serialize_graph(self):
        class TestModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear = torch.nn.Linear(4, 4)
                self.e = torch.rand(4)
                self.conv = torch.nn.Conv2d(3, 3, 2, bias=False)

            def forward(self, a, b, c):
                add_1 = a + b
                conv1 = self.conv(c)
                linear = self.linear(add_1 + conv1)
                add_2 = linear + self.e
                return add_2

        m = TestModule()
        traced = symbolic_trace(m)
        a = torch.rand(4)
        b = torch.rand(4)
        c = torch.rand(3, 3, 2, 2)
        graph_manipulation.get_size_of_all_nodes(traced, [a, b, c])

        partitioner = Partitioner()
        devices = [Device("dev_0", 5000, 0), Device("dev_1", 125, 1)]
        partitioner_config = PartitionerConfig(devices, PartitionMode.sparse_nn)
        ret = partitioner.partition_graph(traced, m, partitioner_config)
        module_with_submodules = ret.module_with_submodules
        # Fix for now to add type/shape to output
        for node in traced.graph.nodes:
            if node.op == "output":
                node.meta["tensor_meta"] = _extract_tensor_metadata(a)
        for mod in module_with_submodules.modules():
            if isinstance(mod, GraphModule):
                for node in mod.graph.nodes:
                    node.meta["tensor_meta"] = _extract_tensor_metadata(a)
        for node in module_with_submodules.graph.nodes:
            node.meta["tensor_meta"] = _extract_tensor_metadata(a)

        weights1 = {}
        weights2 = {}
        serialized_graph1 = graph_manipulation.serialize_module(traced, weights1)
        serialized_graph2 = graph_manipulation.serialize_module(
            module_with_submodules, weights2
        )
        assert len(weights1) == 4
        assert len(weights2) == 4
        assert len(serialized_graph1["nodes"]) == 10
        assert len(serialized_graph1["weights"]) == 4
        assert len(serialized_graph1["modules"]) == 0
        assert len(serialized_graph2["nodes"]) == 6
        assert len(serialized_graph2["weights"]) == 4
        assert len(serialized_graph2["modules"]) == 1
        assert serialized_graph1["weights"]["linear.weight"]["shape"] == "[4, 4]"
        assert serialized_graph1["weights"]["linear.weight"]["dtype"] == "torch.float32"
        assert serialized_graph1["weights"]["linear.weight"]["is_quantized"] is False
        assert serialized_graph1["nodes"][0]["shape"] == "[4]"
        assert serialized_graph1["nodes"][0]["dtype"] == "torch.float32"
        assert serialized_graph1["nodes"][0]["target"] == "a"
        assert serialized_graph1["nodes"][0]["op_code"] == "placeholder"
        assert serialized_graph1["nodes"][0]["name"] == "a"
        assert serialized_graph1["nodes"][6]["args"][0]["name"] == "add_1"
        assert serialized_graph1["nodes"][6]["args"][0]["is_node"] is True

        # Test the users of the nodes. No users of the last/output node.
        assert serialized_graph2["nodes"][0]["users"][0]["name"] == "submod_0"
        assert serialized_graph2["nodes"][1]["users"][0]["name"] == "submod_0"
        assert serialized_graph2["nodes"][4]["users"][0]["name"] == "output"
        assert serialized_graph2["nodes"][5]["users"] == []

        # Test quantization info serialization.
        x = torch.tensor([[-1.0, 0.0], [1.0, 2.0]])
        q_tensor = torch.quantize_per_tensor(x, 1, 0, torch.qint32)
        q_tensor_channel = torch.quantize_per_channel(
            x, torch.tensor([0.1, 0.01]), torch.tensor([10, 0]), 0, torch.quint8
        )
        result, _ = graph_manipulation.serialize_tensor_quantization(
            q_tensor, weights={}, pcq_prefix="foo"
        )
        result2, per_channel_dict = graph_manipulation.serialize_tensor_quantization(
            q_tensor_channel, weights={}, pcq_prefix="bar"
        )
        assert result["qscheme"] == "torch.per_tensor_affine"
        assert result["q_scale"] == 1.0
        assert result2["qscheme"] == "torch.per_channel_affine"
        assert result2["q_per_channel_scales"] == "bar_per_channel_scales"
        assert per_channel_dict["bar_per_channel_zero_points"]["shape"] == "[2]"

    def test_find_single_partition(self):
        class TestModule(torch.nn.Module):
            def forward(self, a, b):
                return a + b

        m = TestModule()
        traced = symbolic_trace(m)
        a = torch.rand(1)
        b = torch.rand(1)
        graph_manipulation.get_size_of_all_nodes(traced, [a, b])
        partitioner = Partitioner()
        devices = [
            Device("dev_0", 125, 0),
            Device("dev_1", 150, 1),
            Device("dev_2", 125, 2),
        ]
        partitioner_config = PartitionerConfig(devices)
        ret = partitioner.partition_graph(traced, m, partitioner_config)
        module_with_submodules = ret.module_with_submodules
        dag = ret.dag
        self.assertEqual(traced(a, b), module_with_submodules(a, b))
        assert dag.nodes[0].logical_device_ids == [1]

    def test_lack_of_devices(self):
        class TestModule(torch.nn.Module):
            def forward(self, a, b):
                return a + b

        m = TestModule()
        traced = symbolic_trace(m)
        a = torch.rand(4)
        b = torch.rand(4)
        graph_manipulation.get_size_of_all_nodes(traced, [a, b])
        partitioner = Partitioner()
        devices = [Device("dev_0", 4, 0), Device("dev_1", 4, 1)]
        partitioner_config = PartitionerConfig(devices, PartitionMode.size_based)
        catch_runtime_error = False
        try:
            ret = partitioner.partition_graph(traced, m, partitioner_config)
        except RuntimeError:
            catch_runtime_error = True
        assert catch_runtime_error

    def test_large_node_error(self):
        class TestModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear = torch.nn.Linear(4, 4)

            def forward(self, a):
                linear = self.linear(a)
                add = linear + a
                return add

        m = TestModule()
        traced = symbolic_trace(m)
        a = torch.rand(4)
        graph_manipulation.get_size_of_all_nodes(traced, [a])
        partitioner = Partitioner()
        devices = [
            Device("dev_0", 40, 0),
            Device("dev_1", 40, 0),
            Device("dev_2", 40, 0),
            Device("dev_3", 40, 0),
            Device("dev_4", 40, 0),
        ]
        partitioner_config = PartitionerConfig(devices, PartitionMode.size_based)
        catch_runtime_error = False
        try:
            ret = partitioner.partition_graph(traced, m, partitioner_config)
        except RuntimeError:
            catch_runtime_error = True
        assert catch_runtime_error

    def test_partition_node_manipulation(self):
        class TestModule(torch.nn.Module):
            def forward(self, a, b):
                add_1 = a + b
                add_2 = add_1 + torch.rand(4)
                add_3 = add_2 + torch.rand(4)
                return add_3

        m = TestModule()
        traced = symbolic_trace(m)
        a, b = torch.rand(4), torch.rand(4)
        graph_manipulation.get_size_of_all_nodes(traced, [a, b])
        partitioner = Partitioner()
        devices = [Device("dev_0", 1000, 0)]
        partitioner_config = PartitionerConfig(devices)
        ret = partitioner.partition_graph(traced, m, partitioner_config)
        partition = partitioner.partitions[0]
        assert partition.used_mem_bytes == 112
        # Select add_2 node to remove
        selected_node = None
        for node in partition.nodes:
            if node.name == "add_2":
                selected_node = node
        partition.remove_node(selected_node)
        assert partition.used_mem_bytes == 80

    def test_size_based_partition(self):
        class TestModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear = torch.nn.Linear(4, 4)
                self.c = torch.rand(4)

            def forward(self, a, b):
                add_1 = a + b
                linear = self.linear(add_1)
                add_2 = linear + self.c
                return add_2

        m = TestModule()
        traced = symbolic_trace(m)
        a = torch.rand(4)
        b = torch.rand(4)
        graph_manipulation.get_size_of_all_nodes(traced, [a, b])
        partitioner = Partitioner()
        devices = [
            Device("dev_0", 125, 0),
            Device("dev_1", 125, 1),
            Device("dev_2", 125, 2),
        ]
        partitioner_config = PartitionerConfig(devices, PartitionMode.size_based)
        ret = partitioner.partition_graph(traced, m, partitioner_config)
        module_with_submodules = ret.module_with_submodules
        dag = ret.dag
        self.assertEqual(traced(a, b), module_with_submodules(a, b))
        for i, node in enumerate(dag.nodes):
            assert node.logical_device_ids == [i]

    def test_partition_device_mapping(self):
        class TestModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear = torch.nn.Linear(4, 4)

            def forward(self, a):
                b = torch.rand(4)
                add_1 = a + b
                linear_1 = self.linear(add_1)
                add_2 = torch.rand(4) + a
                add_3 = add_2 + linear_1
                return add_3

        m = TestModule()
        traced = symbolic_trace(m)
        a = torch.rand(4)
        graph_manipulation.get_size_of_all_nodes(traced, [a])
        partitioner = Partitioner()
        devices = [Device("dev_0", 120, 0), Device("dev_1", 160, 1)]
        partitioner_config = PartitionerConfig(devices, PartitionMode.size_based)
        ret = partitioner.partition_graph(traced, m, partitioner_config)
        module_with_submodules = ret.module_with_submodules
        dag = ret.dag
        self.assertEqual(traced(a), module_with_submodules(a))
        for i, node in enumerate(dag.nodes):
            if i == 1:
                assert node.logical_device_ids == [1]
            else:
                assert node.logical_device_ids == [0]

    def test_sparse_nn_partition(self):
        class MyRecommendationModule(torch.nn.Module):
            def create_mlp(self, num_of_layers: int, input_size: int, output_size: int):
                layers = torch.nn.ModuleList()
                for _ in range(num_of_layers):
                    ll = torch.nn.Linear(input_size, output_size)
                    layers.append(ll)
                    layers.append(torch.nn.ReLU())
                return layers

            def __init__(self):
                super(MyRecommendationModule, self).__init__()
                layers = self.create_mlp(4, 4, 4)
                self.bottom_layers = torch.nn.Sequential(*layers)
                layers = self.create_mlp(3, 24, 24)
                self.top_layers = torch.nn.Sequential(*layers)
                self.embedding_layers = torch.nn.ModuleList()
                el = torch.nn.EmbeddingBag(500000, 4, mode="sum", sparse=True)
                self.embedding_layers.append(el)
                for i in range(3):
                    el = torch.nn.EmbeddingBag(1000000, 4, mode="sum", sparse=True)
                    self.embedding_layers.append(el)
                el = torch.nn.EmbeddingBag(500000, 4, mode="sum", sparse=True)
                self.embedding_layers.append(el)

            def forward(self, a, b, offset):
                x = self.bottom_layers(a)
                y = []
                c = []
                for i in range(len(self.embedding_layers)):
                    temp = torch.randint(10, (8,))
                    c.append(temp + b)
                for i in range(len(self.embedding_layers)):
                    if i % 2 == 0:
                        y.append(self.embedding_layers[i](c[i], offset))
                    else:
                        y.append(
                            self.embedding_layers[i](torch.randint(10, (8,)), offset)
                        )
                z = torch.cat([x] + y, dim=1)
                p = self.top_layers(z)
                return p

        m = MyRecommendationModule()
        a = torch.rand(2, 4)
        b = torch.randint(10, (8,))
        offset = torch.randint(1, (2,))
        traced = symbolic_trace(m)
        graph_manipulation.get_size_of_all_nodes(traced, [a, b, offset])
        devices = [
            Device("dev_0", 33000000, 0),
            Device("dev_1", 33000000, 1),
            Device("dev_2", 33000000, 2),
        ]
        partitioner_config = PartitionerConfig(devices, PartitionMode.sparse_nn)
        partitioner = Partitioner()
        ret = partitioner.partition_graph(traced, m, partitioner_config)
        module_with_submodules = ret.module_with_submodules
        dag = ret.dag
        self.assertEqual(traced(a, b, offset), module_with_submodules(a, b, offset))
        assert len(module_with_submodules.graph.nodes) == 24

    def test_partition_latency(self):
        class TestModule(torch.nn.Module):
            def __init__(self):
                super(TestModule, self).__init__()
                self.linear = torch.nn.Linear(4, 4)

            def forward(self, a):
                add_1 = a + torch.rand(4)
                add_2 = add_1 + torch.rand(4)
                linear_1 = self.linear(add_1)
                add_3 = add_2 + linear_1
                add_4 = add_2 + add_3
                return add_4

        def get_node_to_latency_mapping(fx_module: GraphModule):
            """Given a fx module, generate node latency for each node
            based on the size of each node
            """
            node_to_latency_mapping: Dict[Node, NodeLatency] = {}
            for node in fx_module.graph.nodes:
                if node.op not in {"output", "placeholder", "get_attr"}:
                    if node.size_bytes.total_size == node.size_bytes.output_size:
                        node_to_latency_mapping[node] = NodeLatency(
                            node.size_bytes.total_size, 2.0 * node.size_bytes.total_size
                        )
                    else:
                        node_to_latency_mapping[node] = NodeLatency(
                            node.size_bytes.total_size, node.size_bytes.output_size
                        )
            return node_to_latency_mapping

        m = TestModule()
        traced = symbolic_trace(m)
        a = torch.rand(4)
        graph_manipulation.get_size_of_all_nodes(traced, [a])
        node_to_latency_mapping = get_node_to_latency_mapping(traced)
        devices = [Device("dev_0", 200, 0), Device("dev_1", 200, 1)]
        partitioner = Partitioner()
        partitioner_config = PartitionerConfig(devices)
        ret = partitioner.partition_graph(traced, m, partitioner_config)
        module_with_submodules = ret.module_with_submodules
        self.assertEqual(traced(a), module_with_submodules(a))
        partitions = partitioner.partitions
        partition_to_latency_mapping = get_partition_to_latency_mapping(
            partitions, node_to_latency_mapping
        )
        for p in partition_to_latency_mapping:
            if p.partition_id == 0:
                assert partition_to_latency_mapping[p] == (128.0, 80.0, 160.0)
            else:
                assert partition_to_latency_mapping[p] == (16.0, 32.0, 32.0)
        transfer_rate_bytes_per_sec = 2
        critical_path_latency_sec = get_latency_of_partitioned_graph(
            partitions, partition_to_latency_mapping, transfer_rate_bytes_per_sec
        )
        assert critical_path_latency_sec == 208.0

    def test_cost_aware_partition(self):
        class MyModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear = torch.nn.Linear(4, 4)

            def forward(self, a):
                add_1 = a + torch.rand(4)
                add_2 = add_1 + torch.rand(4)
                linear_1 = self.linear(add_1)
                add_3 = add_2 + torch.rand(4)
                add_4 = add_2 + linear_1
                add_5 = add_3 + add_4
                return add_5

        def get_node_to_latency_mapping(fx_module: GraphModule):
            node_to_latency_mapping: Dict[Node, Nodelatency] = {}
            for node in fx_module.graph.nodes:
                if node.op not in {"output", "placeholder", "get_attr"}:
                    if node.size_bytes.total_size == node.size_bytes.output_size:
                        node_to_latency_mapping[node] = NodeLatency(
                            node.size_bytes.total_size, 1
                        )
                    else:
                        node_to_latency_mapping[node] = NodeLatency(
                            node.size_bytes.total_size, node.size_bytes.output_size
                        )
            return node_to_latency_mapping

        m = MyModule()
        traced = symbolic_trace(m)
        a = torch.rand(4)
        graph_manipulation.get_size_of_all_nodes(traced, [a])
        devices = [
            Device("dev_0", 125, 0),
            Device("dev_1", 125, 1),
            Device("dev_2", 125, 2),
            Device("dev_3", 125, 3),
        ]
        node_to_latency_mapping = get_node_to_latency_mapping(traced)
        partitioner_config = PartitionerConfig(
            devices,
            mode=PartitionMode.cost_aware,
            transfer_rate_bytes_per_sec=2,
            node_to_latency_mapping=node_to_latency_mapping,
        )
        partitioner = Partitioner()
        ret = partitioner.partition_graph(traced, m, partitioner_config)
        module_with_submodules = ret.module_with_submodules
        dag = ret.dag
        self.assertEqual(traced(a), module_with_submodules(a))
        partitions = partitioner.partitions
        partition_to_latency_mapping = get_partition_to_latency_mapping(
            partitions, node_to_latency_mapping
        )
        critical_path_latency_sec = get_latency_of_partitioned_graph(
            partitions,
            partition_to_latency_mapping,
            partitioner_config.transfer_rate_bytes_per_sec,
        )
        assert critical_path_latency_sec == 160.0

        def test_kl_based_partition(self):
            class TestModule(torch.nn.Module):
                def __init__(self):
                    super(TestModule, self).__init__()
                    self.linear = torch.nn.Linear(4, 4)
                    self.b = torch.rand(4)
                    self.c = torch.rand(4)
                    self.d = torch.rand(4)

                def forward(self, a):
                    add_1 = a + self.b
                    add_2 = add_1 + self.c
                    linear_1 = self.linear(add_1)
                    add_3 = add_2 + linear_1
                    add_4 = add_2 + self.d
                    add_5 = add_3 + add_4
                    return add_4

            m = TestModule()
            traced = symbolic_trace(m)
            a = torch.rand(4)
            graph_manipulation.get_size_of_all_nodes(traced, [a])
            node_to_latency_mapping = get_node_to_latency_mapping(traced)
            transfer_rate_bytes_per_sec = 2
            devices = [
                Device("dev_0", 200, 0),
                Device("dev_1", 200, 1),
                Device("dev_2", 200, 2),
                Device("dev_3", 200, 3),
            ]
            partitioner = Partitioner()
            partitioner_config = PartitionerConfig(
                devices,
                mode=PartitionMode.kl_based,
                transfer_rate_bytes_per_sec=transfer_rate_bytes_per_sec,
                node_to_latency_mapping=node_to_latency_mapping,
            )
            ret = partitioner.partition_graph(traced, m, partitioner_config)
            module_with_submodules = ret.module_with_submodules
            self.assertEqual(traced(a), module_with_submodules(a))
            dag = ret.dag
            assert dag.nodes[0] == 176
            assert dag.nodes[1] == 112
            partition_to_latency_mapping = get_partition_to_latency_mapping(
                partitioner.partitions, node_to_latency_mapping
            )
            cost = get_latency_of_partitioned_graph(
                partitioner.partitions,
                partition_to_latency_mapping,
                transfer_rate_bytes_per_sec,
            )
            assert cost == 208.0

        def test_aot_based_partition(self):
            class TestModule(torch.nn.Module):
                def __init__(self):
                    super(TestModule, self).__init__()
                    self.b = torch.rand(4)
                    self.c = torch.rand(4)

                def forward(self, a):
                    add_1 = a + self.b
                    add_2 = self.c + add_1
                    return add_2

            m = TestModule()
            traced = symbolic_trace(m)
            a = torch.rand(4)
            node_to_partition_id = {}
            partition_to_logical_devices = {}
            count = 0
            graph_manipulation.get_size_of_all_nodes(traced, [a])
            for node in traced.graph.nodes:
                if node.op not in {"placeholder", "get_attr", "output"}:
                    node_to_partition_id[node] = count
                    partition_to_logical_devices[count] = [0]
                    count += 1
            devices = [Device("dev_0", 200, 0)]
            partitioner_config = PartitionerConfig(
                devices=devices,
                mode=PartitionMode.aot_based,
                node_to_partition_mapping=node_to_partition_id,
                partition_to_logical_device_mapping=partition_to_logical_devices,
            )
            partitioner = Partitioner()
            ret = partitioner.partition_graph(traced, m, partitioner_config)
            module_with_submodules = ret.module_with_submodules
            dag = ret.dag
            self.assertEqual(module_with_submodules(a), traced(a))
            for node in dag.nodes:
                assert node.size_bytes == 48
                assert node.logical_device_ids == [0]

        def test_replace_target_nodes_with(self):
            class testModule(torch.nn.Module):
                def forward(self, a, b):
                    return a + b

            m = testModule()
            traced = symbolic_trace(m)
            input1 = torch.randn(1)
            input2 = torch.randn(1)
            assert (input1 + input2) == traced(input1, input2)
            graph_manipulation.replace_target_nodes_with(
                fx_module=traced,
                old_op="call_function",
                old_target=operator.add,
                new_op="call_function",
                new_target=operator.mul,
            )
            assert (input1 * input2) == traced(input1, input2)

    def test_saturate_host(self):
        class TestModule(torch.nn.Module):
            def __init__(self):
                super(TestModule, self).__init__()
                self.linear = torch.nn.Linear(4, 4)

            def forward(self, a):
                add_1 = a + torch.rand(4)
                add_2 = add_1 + torch.rand(4)
                linear_1 = self.linear(add_1)
                add_3 = add_2 + linear_1
                add_4 = add_2 + add_3
                return add_4

        m = TestModule()
        traced = symbolic_trace(m)
        a = torch.rand(4)
        graph_manipulation.get_size_of_all_nodes(traced, [a])
        devices = [
            Device("dev_0", 200, 0),
            Device("dev_1", 200, 1),
            Device("dev_2", 100, 2),
            Device("dev_3", 100, 3),
            Device("dev_4", 200, 4),
            Device("dev_5", 100, 5),
        ]
        partitioner = Partitioner()
        # Without host saturation, the model will be split into two partitions.
        # dev_0 holds partition 0 of 192 bytes and dev_1 holds partition 1 of 48 bytes.
        partitioner_config = PartitionerConfig(devices, saturate_host=True)
        ret = partitioner.partition_graph(traced, m, partitioner_config)
        module_with_submodules = ret.module_with_submodules
        self.assertEqual(traced(a), module_with_submodules(a))

        partitions = partitioner.partitions
        self.assertEqual(len(partitions), 2)
        # With host saturation, partition 1 will be replicated to dev_4, and partition 2
        # will be replicated to dev_2.
        self.assertEqual(partitions[0].logical_device_ids, [0, 4])
        self.assertEqual(partitions[1].logical_device_ids, [1, 2])

    @skipIfNoTorchVision
    def test_conv_bn_fusion(self):
        rn18 = resnet18().eval()
        traced = symbolic_trace(rn18)
        fused = optimization.fuse(traced)

        self.assertTrue(
            all(not isinstance(m, torch.nn.BatchNorm2d) for m in fused.modules())
        )

        N, C, H, W = 20, 3, 224, 224
        inp = torch.randn(N, C, H, W)

        self.assertEqual(fused(inp), rn18(inp))

    def test_call_to_assert_no_msg(self):
        class M(torch.nn.Module):
            def forward(self, a, b):
                assert a == b
                return a + b

        m = M()
        traced = symbolic_trace_with_rewrite(m)

        # Make sure the graph is well-formed
        traced.graph.lint()

        # Check the IR to make sure there's a call_function node with target == "Assert"
        self.assertTrue(
            any(
                node.op == "call_function" and node.target == torch._assert
                for node in traced.graph.nodes
            )
        )

        # Ensure that the assert throws when it's supposed to and doesn't throw when it's not supposed to
        traced(3, 3)
        with self.assertRaisesRegex(AssertionError, ""):
            traced(3, 5)

        # Confirm that the output is correct
        self.assertEqual(traced(3, 3), m(3, 3))

    def test_call_to_assert_with_msg(self):
        class M(torch.nn.Module):
            def forward(self, a, b):
                assert a == b, "test message"
                return a + b

        m = M()
        traced = symbolic_trace_with_rewrite(m)

        # Make sure the graph is well-formed
        traced.graph.lint()

        # Check the IR to make sure there's a call_function node with target == "Assert"
        self.assertTrue(
            any(
                node.op == "call_function" and node.target == torch._assert
                for node in traced.graph.nodes
            )
        )

        # Ensure that the assert throws when it's supposed to and doesn't throw when it's not supposed to
        traced(3, 3)
        with self.assertRaisesRegex(AssertionError, "test message"):
            traced(3, 5)

        # Confirm that the output is correct
        self.assertEqual(traced(3, 3), m(3, 3))

    def test_call_to_assert_with_empty_msg(self):
        class M(torch.nn.Module):
            def forward(self, a, b):
                assert a == b, ""
                return a + b

        m = M()
        traced = symbolic_trace_with_rewrite(m)

        # Make sure the graph is well-formed
        traced.graph.lint()

        # Check the IR to make sure there's a call_function node with target == "Assert"
        self.assertTrue(
            any(
                node.op == "call_function" and node.target == torch._assert
                for node in traced.graph.nodes
            )
        )

        # Ensure that the assert throws when it's supposed to and doesn't throw when it's not supposed to
        traced(3, 3)
        with self.assertRaisesRegex(AssertionError, ""):
            traced(3, 5)

        # Confirm that the output is correct
        self.assertEqual(traced(3, 3), m(3, 3))

    def test_call_to_assert_with_multiline_message(self):
        class M(torch.nn.Module):
            def forward(self, a, b):
                error_msg = """
An error message with
terrible spacing
                """
                assert a == b, error_msg
                return a + b

        m = M()
        traced = symbolic_trace_with_rewrite(m)

        # Make sure the graph is well-formed
        traced.graph.lint()

        # Check the IR to make sure there's a call_function node with target == "Assert"
        self.assertTrue(
            any(
                node.op == "call_function" and node.target == torch._assert
                for node in traced.graph.nodes
            )
        )

        # Ensure that the assert throws when it's supposed to and doesn't throw when it's not supposed to
        error_msg = """
An error message with
terrible spacing
    """
        traced(3, 3)
        with self.assertRaisesRegex(AssertionError, error_msg):
            traced(3, 5)

        # Confirm that the output is correct
        self.assertEqual(traced(3, 3), m(3, 3))

    def test_subgraph_creation(self):
        class MyModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.param = torch.nn.Parameter(torch.rand(3, 4))
                self.linear = torch.nn.Linear(4, 5)

            def forward(self, x, y):
                z = self.linear(x + self.param).clamp(min=0.0, max=1.0)
                w = self.linear(y).clamp(min=0.0, max=1.0)
                return z + w

        # symbolically trace model
        my_module = MyModule()
        my_module_traced = symbolic_trace(my_module)

        # random mod partitioning
        partition_counter = 0
        NPARTITIONS = 3

        # Add some random meta info to make sure it is kept around.
        for node in my_module_traced.graph.nodes:
            if node.op != "output":
                node.meta["test_meta_info"] = True

        def mod_partition(node: Node):
            nonlocal partition_counter
            partition = partition_counter % NPARTITIONS
            partition_counter = (partition_counter + 1) % NPARTITIONS
            return partition

        # split module in module with submodules
        module_with_submodules = split_module(
            my_module_traced, my_module, mod_partition
        )

        # Check that test_meta_info was still on all nodes.
        submodules = dict(module_with_submodules.named_modules())
        for node in module_with_submodules.graph.nodes:
            if node.op == "call_module":
                submod = submodules[node.target]
                self.assertTrue(isinstance(submod, torch.fx.GraphModule))
                for submod_node in submod.graph.nodes:
                    if submod_node.op != "output":
                        stored_op = submod_node.meta.get("test_meta_info")
                        self.assertTrue(stored_op is not None and stored_op)

        x = torch.rand(3, 4)
        y = torch.rand(3, 4)

        orig_out = my_module_traced(x, y)
        submodules_out = module_with_submodules(x, y)

        self.assertEqual(orig_out, submodules_out)

    @skipIfNoTorchVision
    def test_subgraph_trivial_resnet(self):
        # Smoke test trivially splitting resnet into 1 partition works
        # There was an issue before causing submodule names to be aliased
        m = resnet18()
        traced = symbolic_trace(m)
        a = torch.rand(64, 3, 7, 7)
        module_with_submodules = split_module(traced, m, lambda node: 0)
        module_with_submodules(a)

    def test_normalize_binary_operators(self):
        ops_to_test = {
            torch.add,
            torch.mul,
            torch.sub,
            torch.div,
            torch.floor_divide,
            torch.remainder,
            torch.eq,
            torch.ne,
            torch.lt,
            torch.le,
            torch.gt,
            torch.ge,
        }

        # Test Tensor/Tensor callsite
        for op in ops_to_test:

            class WrapperMod(torch.nn.Module):
                def forward(self, x, y):
                    return op(x, y)

            traced = symbolic_trace(WrapperMod())
            normalized = NormalizeOperators(traced).transform()
            x, y = torch.randn(3, 4), torch.randn(3, 4)
            torch.testing.assert_close(traced(x, y), normalized(x, y))
            self.assertFalse(
                any(n.target in ops_to_test for n in normalized.graph.nodes)
            )

        # Test Tensor/scalar callsite
        for op in ops_to_test:

            class WrapperMod(torch.nn.Module):
                def forward(self, x):
                    return op(x, 42)

            traced = symbolic_trace(WrapperMod())
            normalized = NormalizeOperators(traced).transform()
            x = torch.randn(3, 4)
            torch.testing.assert_close(traced(x), normalized(x))
            self.assertFalse(
                any(n.target in ops_to_test for n in normalized.graph.nodes)
            )

    @skipIfNoTorchVision
    def test_normalize_args(self):
        m = resnet18()

        class FunctionalTracer(torch.fx.Tracer):
            def is_leaf_module(
                self, m: torch.nn.Module, module_qualified_name: str
            ) -> bool:
                # `leaves` contains the set of standard `nn.Modules` that are not
                # currently symbolically traceable. Ideally this set would be empty
                leaves = set([torch.nn.BatchNorm2d])
                return type(m) in leaves

        traced = torch.fx.GraphModule(m, FunctionalTracer().trace(m))

        input = torch.randn(5, 3, 224, 224)
        ref_outs = traced(input)

        ShapeProp(traced).propagate(input)
        traced = NormalizeArgs(traced).transform()

        modules = dict(traced.named_modules())

        for node in traced.graph.nodes:
            if node.op == "call_function" and node.target != operator.add:
                self.assertEqual(len(node.args), 0)
            elif node.op == "call_module":
                submod_class = modules[node.target].__class__
                nn_class = getattr(torch.nn, submod_class.__name__)
                if submod_class == nn_class:
                    self.assertEqual(len(node.args), 0)
        traced(input)
        self.assertEqual(traced(input), ref_outs)

    def test_normalize_modules_exhaustive(self):
        """
        Exhaustively test `Node.normalized_arguments` on all standard
        torch.nn Module classes
        """
        for test_params in module_tests + new_module_tests:
            if "constructor" not in test_params:
                constructor = getattr(torch.nn, test_params["module_name"])
            else:
                constructor = test_params["constructor"]

            if "constructor_args" not in test_params:
                args = ()
            else:
                args = test_params["constructor_args"]

            mod = constructor(*args)
            # Skip modules that are not standard `torch.nn`
            # instances, including functionals. (functionals
            # are tested in test_normalize_args)
            if mod.__class__.__name__ not in dir(torch.nn):
                continue

            if "input_fn" not in test_params:
                inputs = torch.randn(test_params["input_size"])
            else:
                inputs = test_params["input_fn"]()

            if not isinstance(inputs, (tuple, list)):
                inputs = (inputs,)

            params = ", ".join(f"v{i}" for i in range(len(inputs)))

            # Generate a class to wrap this standard `nn.Module` instance
            test_classname = f"Test{mod.__class__.__name__}"
            test_mod_code = f"""
class {test_classname}(torch.nn.Module):
    def __init__(self, mod):
        super().__init__()
        self.mod = mod

    def forward(self, {params}):
        return self.mod({params})
            """

            gbls = {"torch": torch}
            exec(test_mod_code, gbls)

            test_instance = gbls[test_classname](mod)
            traced = symbolic_trace(test_instance)

            # Use `Node.normalized_arguments` to get a new set of arguments
            # to feed to the Module. Then, rewrite the node to only take
            # in those arguments as kwargs
            modules = dict(traced.named_modules())
            for node in traced.graph.nodes:
                if node.op == "call_module":
                    submod_class = modules[node.target].__class__
                    nn_class = getattr(torch.nn, submod_class.__name__)
                    if submod_class == nn_class:
                        normalized_args = node.normalized_arguments(traced)
                        normalized_args2 = normalize_module(
                            traced, node.target, node.args, node.kwargs
                        )
                        assert normalized_args == normalized_args2
                        assert normalized_args
                        node.args = normalized_args.args
                        node.kwargs = normalized_args.kwargs

            traced.recompile()

            # These Modules have an RNG in their forward, so testing
            # correctness by comparing outputs is not correct. Skip that
            # check for these
            stochastic_modules = {"FractionalMaxPool2d", "FractionalMaxPool3d", "RReLU"}

            if mod.__class__.__name__ not in stochastic_modules:
                self.assertEqual(traced(*inputs), mod(*inputs))

            traced = NormalizeArgs(symbolic_trace(test_instance)).transform()
            modules = dict(traced.named_modules())
            for node in traced.graph.nodes:
                if node.op == "call_module":
                    submod_class = modules[node.target].__class__
                    nn_class = getattr(torch.nn, submod_class.__name__)
                    if submod_class == nn_class:
                        self.assertEqual(len(node.args), 0)

    def test_normalize_args_preserve_meta(self):
        class MyModule(torch.nn.Module):
            def __init__(self):
                super().__init__()

            def forward(self, a):
                return torch.add(a, 3)

        m = MyModule()
        traced = symbolic_trace(m)

        for node in traced.graph.nodes:
            if node.op == "call_function" and node.target == torch.add:
                node.meta["my_key"] = 7
                break
        else:
            self.fail("Didn't find call_function torch.add")

        input = torch.randn(2, 3)
        ShapeProp(traced).propagate(input)
        traced = NormalizeArgs(traced).transform()

        for node in traced.graph.nodes:
            if node.op == "call_function" and node.target == torch.add:
                self.assertTrue("my_key" in node.meta)
                self.assertEqual(node.meta["my_key"], 7)
                break
        else:
            self.fail("Didn't find call_function torch.add")

    @skipIfNoTorchVision
    def test_annotate_returns_with_schema(self):
        m = resnet18()

        traced_modules = symbolic_trace(m)
        traced_modules_annotated = AnnotateTypesWithSchema(traced_modules).transform()
        for node in traced_modules_annotated.graph.nodes:
            if node.type is None:
                check = (node.op, node.target)
                self.assertIn(
                    check,
                    {
                        ("placeholder", "x"),
                        ("call_module", "maxpool"),
                        ("call_function", operator.add),
                        ("call_function", torch.flatten),
                        ("output", "output"),
                    }
                )

        # Smoke test torchscript compilation since now we're emitting type annotations
        torch.jit.script(traced_modules_annotated)

        class FunctionalTracer(torch.fx.Tracer):
            def is_leaf_module(
                self, m: torch.nn.Module, module_qualified_name: str
            ) -> bool:
                # `leaves` contains the set of standard `nn.Modules` that are not
                # currently symbolically traceable. Ideally this set would be empty
                leaves = set([torch.nn.BatchNorm2d])
                return type(m) in leaves

        traced_functionals = torch.fx.GraphModule(m, FunctionalTracer().trace(m))

        traced_functionals_annotated = AnnotateTypesWithSchema(
            traced_functionals
        ).transform()
        for node in traced_functionals_annotated.graph.nodes:
            if node.type is None:
                check = (node.op, node.target)
                excluded_nodes = {
                    ("placeholder", "x"),
                    ("call_function", torch.conv2d),
                    # Return type differs based on boolean dispatch :(
                    ("call_function", torch.nn.functional.max_pool2d),
                    ("call_function", operator.add),
                    ("call_function", torch.flatten),
                    ("output", "output"),
                }
                self.assertIn(check, excluded_nodes)

        # Smoke test torchscript compilation since now we're emitting type annotations
        torch.jit.script(traced_functionals_annotated)

    def test_subgraph_uniquename(self):
        class MyModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear = torch.nn.Linear(4, 4)

            def forward(self, a, b, c, d):
                add_1 = a + b
                add_2 = add_1 + c
                linear_1 = self.linear(add_1)
                add_3 = add_2 + d
                add_4 = add_2 + linear_1
                add_5 = add_3 + add_4
                return add_5

        a, b, c, d = torch.ones(4), torch.ones(4), torch.ones(4), torch.ones(4)
        mm = MyModule()
        traced = symbolic_trace(mm)

        def split_cb(node: torch.fx.Node):
            if node.name == "a" or node.name == "b" or node.name == "add":
                return 0
            else:
                return 1

        module_with_submodule = split_module(traced, mm, split_cb)
        self.assertEqual(module_with_submodule(a, b, c, d), traced(a, b, c, d))

    def test_traceable_function_with_nonstandard_name(self):
        def foo(x):
            return torch.relu(x)

        traced = symbolic_trace_with_rewrite(foo)

    def test_to_folder(self):
        class Test(torch.nn.Module):
            def __init__(self):
                super(Test, self).__init__()
                self.W = torch.nn.Parameter(torch.randn(2))
                self.seq = torch.nn.Sequential(torch.nn.BatchNorm1d(2, 2))
                self.linear = torch.nn.Linear(2, 2)
                self.attr = torch.randn(2)
                self.register_buffer("attr2", torch.randn(2))

            def forward(self, x):
                return self.linear(self.seq(self.W + self.attr + self.attr2 + x))

        mod = symbolic_trace(Test())
        module_name = "Foo"
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_dir = Path(tmp_dir)
            mod.to_folder(tmp_dir, module_name)
            # Recipe taken from here:
            # https://docs.python.org/3/library/importlib.html#importing-a-source-file-directly
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                module_name, tmp_dir / "__init__.py"
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            t = torch.randn(2, 2)
            self.assertEqual(module.Foo()(t), mod(t))

    def test_fetch(self):
        attrs_for_lowering: Dict[str, List[str]] = {
            "torch.nn.modules.conv.Conv2d": [
                "weight",
                "bias",
                "kernel_size",
                "stride",
                "padding",
                "dilation",
                "groups",
                "padding_mode",
            ],
            "torch.nn.modules.batchnorm.BatchNorm2d": [
                "weight",
                "bias",
                "running_mean",
                "running_var",
                "eps",
            ],
        }

        class TestModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.conv = torch.nn.Conv2d(3, 3, 2)
                self.bn = torch.nn.BatchNorm2d(3)

            def forward(self, a):
                a = self.conv(a)
                a += a
                return self.bn(a)

        mod = TestModule()
        traced = symbolic_trace(mod)
        lift_lowering_attrs_to_nodes(traced)

        for node in traced.graph.nodes:
            if node.op == "call_module":
                assert hasattr(node, "attrs_for_lowering")
                para_list = attrs_for_lowering[node.attrs_for_lowering["name"]]

                # node.attrs_for_lowering has an addition field of class name
                assert len(para_list) + 1 == len(node.attrs_for_lowering)
                for p_name in para_list:
                    assert p_name in node.attrs_for_lowering

    def test_merge_matmuls(self):
        """
        A collection of test cases for torch.fx.experimental.merge_matmul,
        a graph transformation that merges matrix multiplication operations.
        """
        # Utility function for counting matmuls for test assertions.
        def _count_matmuls(mod):
            gm = torch.fx.symbolic_trace(mod)

            num_matmuls = 0
            for node in gm.graph.nodes:
                if node.target == torch.matmul:
                    num_matmuls += 1

            return num_matmuls

        # Simple test case in which there are two matmuls of the same size to merge.
        class SimpleMergeMatmulModule(torch.nn.Module):
            def __init__(self, rhs):
                super().__init__()
                self.rhs = rhs

            def forward(self, x, y):
                a = torch.matmul(x, self.rhs)
                b = torch.matmul(y, self.rhs)
                return a + b

        # Initialize inputs.
        a = torch.randn(3, 3)
        b = torch.randn(3, 3)

        # Initialize RHS for matmuls.
        rhs = torch.randn(3, 4)

        # Construct SimpleMergeMatmulModule and call merge_matmul on it.
        module = SimpleMergeMatmulModule(rhs)
        opt_module = merge_matmul.merge_matmul(module)

        # Numerical correctness check.
        before = module(a, b)
        after = opt_module(a, b)
        before.allclose(after)

        # Basic graph structure check; original module should have 2 matmuls
        # and optimized module should have 1.
        self.assertEqual(_count_matmuls(module), 2)
        self.assertEqual(_count_matmuls(opt_module), 1)

        # Test case in which there are multiple matmuls of different sizes to merge.
        class FiveMergeMatmulModule(torch.nn.Module):
            def __init__(self, rhs):
                super().__init__()
                self.rhs = rhs

            def forward(self, a, b, c, d, e):
                s = torch.tensor([])
                matmuls = []

                # For some reason using a list comprehension or for-loop for this
                # doesn't work.
                matmuls.append(torch.matmul(a, self.rhs))
                matmuls.append(torch.matmul(b, self.rhs))
                matmuls.append(torch.matmul(c, self.rhs))
                matmuls.append(torch.matmul(d, self.rhs))
                matmuls.append(torch.matmul(e, self.rhs))

                for m in matmuls:
                    s += torch.sum(m)

                return s

        # Initialize inputs.
        inputs = [torch.randn(2 * i + 1, 5) for i in range(5)]

        # Initialize RHS.
        rhs = torch.randn(5, 4)

        # Construct FiveMergeMatmulModule and call merge_matmul on it.
        module = FiveMergeMatmulModule(rhs)
        opt_module = merge_matmul.merge_matmul(module)

        # Numerical correctness check.
        before = module(*inputs)
        after = opt_module(*inputs)
        before.allclose(after)

        # Basic graph structure check; original module should have len(inputs) matmuls
        # and optimized module should have 1.
        self.assertEqual(_count_matmuls(module), len(inputs))
        self.assertEqual(_count_matmuls(opt_module), 1)

        # Simple test case in which two matmuls cannot be merged due to a data dependency between
        # the LHS operands.
        class UnmergeableMatmulModule(torch.nn.Module):
            def __init__(self, rhs):
                super().__init__()
                self.rhs = rhs

            def forward(self, x):
                a = torch.matmul(x, self.rhs)
                a_abs = torch.abs(a)
                b = torch.matmul(a_abs.transpose(1, 0), self.rhs)
                return b

        # Initialize inputs.
        a = torch.randn(3, 3)

        # Initialize RHS for matmuls.
        rhs = torch.randn(3, 4)

        # Construct UnmergeableMatmulModule and call merge_matmul on it.
        module = UnmergeableMatmulModule(rhs)
        opt_module = merge_matmul.merge_matmul(module)

        # Numerical correctness check.
        before = module(a)
        after = opt_module(a)
        before.allclose(after)

        # Basic graph structure check; the number of matrix multiplcations should not have changed.
        self.assertEqual(_count_matmuls(module), 2)
        self.assertEqual(_count_matmuls(opt_module), 2)

    def test_type_matches(self):
        should_be_equal = [
            (int, type(5)),
            (numbers.Number, type(5)),
            (numbers.Number, type(5.0)),
            (int, type(torch.float)),
            (Union[int, float], type(5)),
            (Union[int, float], type(5.0)),
            (List[int], type(5)),
            (List[int], create_type_hint([int, int])),
            (List[int], create_type_hint((int, int))),
            (List[torch.Tensor], create_type_hint([torch.Tensor, torch.Tensor])),
            (
                List[torch.Tensor],
                create_type_hint([torch.nn.Parameter, torch.nn.Parameter]),
            ),
            (torch.Tensor, torch.nn.Parameter),
            (List[torch.Tensor], create_type_hint([torch.nn.Parameter, torch.Tensor])),
            (List[torch.Tensor], create_type_hint([torch.Tensor, torch.nn.Parameter])),
            (List[torch.Tensor], create_type_hint((torch.Tensor, torch.Tensor))),
            (
                List[torch.Tensor],
                create_type_hint((torch.nn.Parameter, torch.nn.Parameter)),
            ),
            (torch.Tensor, torch.nn.Parameter),
            (List[torch.Tensor], create_type_hint((torch.nn.Parameter, torch.Tensor))),
            (List[torch.Tensor], create_type_hint((torch.Tensor, torch.nn.Parameter))),
            (Optional[List[torch.Tensor]], List[torch.Tensor]),
            (Optional[List[int]], List[int]),
        ]
        for sig_type, arg_type in should_be_equal:
            self.assertTrue(type_matches(sig_type, arg_type))

        should_fail = [
            (int, float),
            (Union[int, float], str),
            (List[torch.Tensor], List[int]),
        ]

        for sig_type, arg_type in should_fail:
            self.assertFalse(type_matches(sig_type, arg_type))

    @skipIfNoMkldnn
    def test_optimize_for_inference_cpu(self):
        import torch.nn as nn

        class Foo(nn.Module):
            def __init__(self):
                super().__init__()
                layers = []
                layers2 = []
                for _ in range(10):
                    layers.append(nn.Conv2d(3, 3, 1))
                    layers.append(nn.BatchNorm2d(3))
                    layers.append(nn.ReLU())

                    layers2.append(nn.Conv2d(3, 3, 1))
                    layers2.append(nn.BatchNorm2d(3))
                    layers2.append(nn.ReLU())
                self.model = nn.Sequential(*layers)
                self.model2 = nn.Sequential(*layers2)

            def forward(self, x):
                return self.model(x) + self.model2(x)

        N, C, H, W, = (
            1,
            3,
            224,
            224,
        )
        inp = torch.randn(N, C, H, W)
        with torch.no_grad():
            model = Foo().eval()
            optimized_model = optimization.optimize_for_inference(model)
            torch.testing.assert_close(model(inp), optimized_model(inp))

            optimized_model2 = optimization.optimize_for_inference(
                model, pass_config={"remove_dropout": False}
            )
            torch.testing.assert_close(model(inp), optimized_model2(inp))

    @skipIfNoTorchVision
    @skipIfNoMkldnn
    def test_optimize_for_inference_cpu_torchvision(self):
        models = [
            torchvision.models.resnet18,
            torchvision.models.resnet50,
            torchvision.models.densenet121,
            torchvision.models.shufflenet_v2_x1_0,
            torchvision.models.vgg16,
            torchvision.models.mobilenet_v2,
            torchvision.models.mnasnet1_0,
            torchvision.models.resnext50_32x4d,
        ]
        with torch.no_grad():
            for model_type in models:
                model = model_type()
                C, H, W, = (
                    3,
                    224,
                    224,
                )
                inp = torch.randn(3, C, H, W)
                model(inp)
                model.eval()
                inp = torch.randn(1, C, H, W)
                heuristic = optimization.gen_mkl_autotuner(inp, iters=0, warmup=0)
                optimized_model = optimization.optimize_for_inference(model)

                orig_out = model(inp)
                new_out = optimized_model(inp)
                torch.testing.assert_close(orig_out, new_out)


class TestNormalizeOperators(JitTestCase):
    @onlyCPU
    @ops(op_db, allowed_dtypes=(torch.float,))
    def test_normalize_operator_exhaustive(self, device, dtype, op):
        # Sorted and one entry on each line to minimize merge conflicts.
        op_skip = {
            # See: https://github.com/pytorch/pytorch/issues/64997
            "block_diag",
            "broadcast_tensors",
            "contiguous",
            "einsum",
            "expand",
            "expand_as",
            "fill_",
            "T",   # Implemented with a lambda
            "H",   # Implemented with a lambda
            "mT",  # Implemented with a lambda
            "mH",  # Implemented with a lambda
            "gradient",
            "histogramdd",
            "igamma",
            "igammac",
            "index_put",
            "nn.functional.conv2d",
            "nn.functional.dropout",
            "nn.functional.embedding",  # Implemented with a lambda
            "polygamma",
            "special.polygamma",
            "repeat",
            "reshape_as",
            "resize_",
            "resize_as_",
            "special.zeta",
            "to_sparse",
            "view",
            "view_as",
            "unfold",
            "where",
            "zero_",
            'bfloat16',
            'bool',
            'byte',
            'char',
            'double',
            'float',
            'half',
            'int',
            'long',
            'short',
            'empty_like',
            'ones_like',
            'randn_like',
            'zeros_like',
            'full_like',
            "__getitem__",
            "__radd__",
            "__rsub__",
            "__rmul__",
            "__rdiv__",
            "__rmod__",
            "__rpow__",
            '__rand__',
            '__ror__',
            '__rxor__',
            "__rmatmul__",
        }

        # Unsupported input types
        if op.name in op_skip:
            return

        if op.name.startswith('_masked.'):
            return

        # These ops currently don't trace in FX for various reasons (i.e. they take a list of tensors)
        fx_fail = {"cat", "stack", "hstack", "vstack", "dstack", "linalg.multi_dot"}
        sample_inputs_itr = op.sample_inputs(device, dtype, requires_grad=False)
        for sample_input in sample_inputs_itr:
            unsupported_arg_type = False
            arg_values = [sample_input.input] + list(sample_input.args)
            kwarg_values = sample_input.kwargs
            arg_types = []
            kwarg_types = {}

            def jit_infer_type(v):
                inferred_arg_type = torch._C._jit_try_infer_type(v)
                assert inferred_arg_type.success()
                t = _torchscript_type_to_python_type(inferred_arg_type.type())
                return t

            for v in arg_values:
                if isinstance(v, torch.Tensor):
                    arg_types.append(type(v))
                else:
                    if isinstance(v, complex):
                        # Complex type not supported in FX
                        unsupported_arg_type = True
                    arg_types.append(jit_infer_type(v))

            for k, v in kwarg_values.items():
                if isinstance(v, torch.Tensor):
                    kwarg_types[k] = type(v)
                else:
                    if isinstance(v, complex):
                        # Complex type not supported in FX
                        unsupported_arg_type = True
                    kwarg_types[k] = jit_infer_type(v)

            if unsupported_arg_type:
                continue
            # Test normalize_function by itself
            ref_out = op.op(*arg_values, **kwarg_values)
            norm_args_and_kwargs = normalize_function(
                op.op, arg_values, kwarg_values, arg_types, kwarg_types
            )
            if norm_args_and_kwargs is None:
                raise RuntimeError(
                    """
                    FX failed to normalize op - add the op to the op_skip list.
                    A common reason is if your OpInfo was implemented with a lambda
                    - otherwise, file an issue
                    """
                )
            test_out = op.op(*norm_args_and_kwargs.args, **norm_args_and_kwargs.kwargs)
            self.assertEqual(test_out, ref_out)

            # Test normalized_arguments as part of FX
            if op.name in fx_fail:
                continue
            param_names = []
            param_values = []
            fx_args = []
            for idx, v in enumerate(arg_values):
                if isinstance(v, torch.Tensor):
                    param_names.append(f"arg_{idx}")
                    param_values.append(v)
                    fx_args.append(param_names[-1])
                else:
                    fx_args.append(f"{repr(v)}")

            for k, v in kwarg_values.items():
                if isinstance(v, torch.Tensor):
                    param_names.append(k)
                    param_values.append(v)
                    fx_args.append(f"{k} = {k}")
                else:
                    fx_args.append(f"{k} = {repr(v)}")

            code = f"""
class TestModule(torch.nn.Module):
    def forward(self, {', '.join(param_names)}):
        return torch.{op.name}({', '.join(fx_args)})
            """

            g = {"torch": torch, "inf": math.inf}
            exec(code, g)
            TestModule = g["TestModule"]

            m = TestModule()
            traced = torch.fx.symbolic_trace(m)
            ref_out = traced(*param_values)

            for node in traced.graph.nodes:
                if node.op == "call_function":
                    normalized_args = node.normalized_arguments(
                        traced, arg_types, kwarg_types
                    )
                    assert normalized_args
                    node.args = normalized_args.args
                    node.kwargs = normalized_args.kwargs
            traced.recompile()

            test_out = traced(*param_values)
            self.assertEqual(test_out, ref_out)


instantiate_device_type_tests(TestNormalizeOperators, globals())

if __name__ == "__main__":
    run_tests()
