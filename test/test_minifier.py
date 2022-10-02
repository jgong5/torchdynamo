#!/usr/bin/env pytest
import os
import shutil
from unittest.mock import patch

import torch

import torchdynamo
import torchdynamo.testing
from torchdynamo.optimizations.backends import create_backend


class MockModule(torch.nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, x):
        for _ in range(10):
            x = torch.sin(x)
        x = torch._foobar(x)
        for _ in range(10):
            x = torch.cos(x)
        return x


class MinfierTests(torchdynamo.testing.TestCase):
    def test_after_dynamo(self):
        @create_backend
        def bad_dynamo_backend(subgraph):
            import sys

            def f(*args):
                # Shifted the forced exception to runtime as this is more common
                # in JIT compilers.
                for node in subgraph.model.graph.nodes:
                    if node.op == "call_function" and node.target is torch._foobar:
                        sys.stdout.write("Dynamo compiled failed\n")
                        raise NotImplementedError("foobar is not implemented")
                return subgraph.model(*args)

            return f

        mod = MockModule()
        opt_mod = torchdynamo.optimize("bad_dynamo_backend")(mod)
        repro_dir = "/tmp/test_minifier"
        repro_file = os.path.join(repro_dir, "minifier_launcher.py")
        shutil.rmtree(repro_dir, ignore_errors=True)

        @patch.object(torchdynamo.config, "repro_after", "dynamo")
        @patch.object(torchdynamo.config, "repro_dir", repro_dir)
        def inner():
            x = torch.randn(4)
            try:
                opt_mod(x)
            except Exception:
                pass

        inner()
        self.assertTrue(os.path.exists(repro_file))

    def test_after_aot(self):
        mod = MockModule()
        opt_mod = torchdynamo.optimize("inductor")(mod)
        repro_dir = "/tmp/test_minifier"
        repro_file = os.path.join(repro_dir, "minifier_launcher.py")
        shutil.rmtree(repro_dir, ignore_errors=True)

        @patch.object(torchdynamo.config, "repro_after", "aot")
        @patch.object(torchdynamo.config, "repro_dir", repro_dir)
        def inner():
            x = torch.randn(4)
            try:
                opt_mod(x)
            except Exception:
                pass

        inner()

        self.assertTrue(os.path.exists(repro_file))
