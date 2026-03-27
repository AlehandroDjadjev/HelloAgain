import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase, override_settings

from apps.agent_core.llm_client import (
    LLMClient,
    LLMError,
    _find_complete_local_snapshot,
    _is_vl_model_config,
    _inspect_local_model_path,
    _placement_verdict,
    _tokenizer_attr,
    _transformers_max_new_tokens,
)


class LLMClientConfigTests(SimpleTestCase):
    @override_settings(
        LOCAL_LLM_PROVIDER="transformers",
        LOCAL_LLM_MODEL="Qwen/Qwen3-14B",
        LOCAL_LLM_API_KEY="",
        LOCAL_LLM_BASE_URL="",
        LOCAL_LLM_TIMEOUT=61,
    )
    def test_from_reasoning_provider_local_uses_local_settings(self):
        client = LLMClient.from_reasoning_provider("local")

        self.assertEqual(client.provider, "transformers")
        self.assertEqual(client.model, "Qwen/Qwen3-14B")
        self.assertEqual(client.api_key, "")
        self.assertEqual(client.base_url, "")
        self.assertEqual(client.timeout, 61)

    @override_settings(
        OPENAI_LLM_MODEL="gpt-5-mini",
        OPENAI_LLM_API_KEY="sk-test",
        OPENAI_LLM_BASE_URL="https://api.openai.com/v1",
        OPENAI_LLM_TIMEOUT=27,
    )
    def test_from_reasoning_provider_openai_uses_openai_settings(self):
        client = LLMClient.from_reasoning_provider("openai")

        self.assertEqual(client.provider, "openai")
        self.assertEqual(client.model, "gpt-5-mini")
        self.assertEqual(client.api_key, "sk-test")
        self.assertEqual(client.base_url, "https://api.openai.com/v1")
        self.assertEqual(client.timeout, 27)

    def test_unknown_reasoning_provider_raises(self):
        with self.assertRaises(LLMError):
            LLMClient.from_reasoning_provider("mystery")


class LLMClientLocalSnapshotTests(SimpleTestCase):
    def test_inspect_local_model_path_reports_complete_snapshot(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text("{}")
            (root / "tokenizer.json").write_text("{}")
            (root / "tokenizer_config.json").write_text("{}")
            (root / "generation_config.json").write_text("{}")
            (root / "model-00001-of-00002.safetensors").write_bytes(b"a" * 8)
            (root / "model-00002-of-00002.safetensors").write_bytes(b"b" * 12)
            (root / "model.safetensors.index.json").write_text(
                json.dumps(
                    {
                        "weight_map": {
                            "layer0": "model-00001-of-00002.safetensors",
                            "layer1": "model-00002-of-00002.safetensors",
                        }
                    }
                )
            )

            snapshot = _inspect_local_model_path(root)

        self.assertTrue(snapshot.is_complete)
        self.assertEqual(snapshot.shard_count, 2)
        self.assertEqual(snapshot.missing_shards, [])

    def test_find_complete_local_snapshot_prefers_complete_revision(self):
        with TemporaryDirectory() as tmp:
            repo = (
                Path(tmp)
                / "hub"
                / "models--Qwen--Qwen3-14B"
                / "snapshots"
            )
            incomplete = repo / "111"
            complete = repo / "222"
            incomplete.mkdir(parents=True)
            complete.mkdir(parents=True)

            (incomplete / "config.json").write_text("{}")
            (incomplete / "tokenizer.json").write_text("{}")
            (incomplete / "tokenizer_config.json").write_text("{}")
            (incomplete / "model.safetensors.index.json").write_text(
                json.dumps({"weight_map": {"layer0": "model-00001-of-00002.safetensors"}})
            )

            (complete / "config.json").write_text("{}")
            (complete / "tokenizer.json").write_text("{}")
            (complete / "tokenizer_config.json").write_text("{}")
            (complete / "model-00001-of-00002.safetensors").write_bytes(b"a")
            (complete / "model.safetensors.index.json").write_text(
                json.dumps({"weight_map": {"layer0": "model-00001-of-00002.safetensors"}})
            )

            with override_settings():
                old = os.environ.get("HF_HOME")
                os.environ["HF_HOME"] = tmp
                try:
                    snapshot = _find_complete_local_snapshot("Qwen/Qwen3-14B")
                finally:
                    if old is None:
                        os.environ.pop("HF_HOME", None)
                    else:
                        os.environ["HF_HOME"] = old

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.revision, "222")
        self.assertTrue(snapshot.is_complete)

    def test_placement_verdict_reports_full_gpu_without_device_map(self):
        class DummyModel:
            device = "cuda:0"

        self.assertEqual(_placement_verdict(DummyModel()), "FULL_GPU")

    def test_placement_verdict_reports_disk_offload(self):
        class DummyModel:
            hf_device_map = {"layer0": 0, "layer1": "cpu", "layer2": "disk"}

        self.assertEqual(_placement_verdict(DummyModel()), "DISK_OFFLOAD")

    def test_transformers_max_new_tokens_prefers_smaller_json_budget(self):
        old_json = os.environ.get("LOCAL_LLM_JSON_MAX_NEW_TOKENS")
        old_default = os.environ.get("LOCAL_LLM_MAX_NEW_TOKENS")
        os.environ["LOCAL_LLM_JSON_MAX_NEW_TOKENS"] = "96"
        os.environ["LOCAL_LLM_MAX_NEW_TOKENS"] = "224"
        try:
            self.assertEqual(_transformers_max_new_tokens(True), 96)
            self.assertEqual(_transformers_max_new_tokens(False), 224)
        finally:
            if old_json is None:
                os.environ.pop("LOCAL_LLM_JSON_MAX_NEW_TOKENS", None)
            else:
                os.environ["LOCAL_LLM_JSON_MAX_NEW_TOKENS"] = old_json
            if old_default is None:
                os.environ.pop("LOCAL_LLM_MAX_NEW_TOKENS", None)
            else:
                os.environ["LOCAL_LLM_MAX_NEW_TOKENS"] = old_default

    def test_tokenizer_attr_falls_back_to_nested_tokenizer(self):
        class NestedTokenizer:
            eos_token_id = 42

        class ProcessorLike:
            tokenizer = NestedTokenizer()

        self.assertEqual(_tokenizer_attr(ProcessorLike(), "eos_token_id"), 42)

    def test_is_vl_model_config_detects_qwen2_vl(self):
        class DummyConfig:
            model_type = "qwen2_vl"

        self.assertTrue(_is_vl_model_config(DummyConfig()))
