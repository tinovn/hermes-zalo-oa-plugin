"""Phần module-level của adapter: cắt tin dài, mode bảo trì, cờ env.

``adapter.py`` import ``gateway.*`` (chỉ có trong bản cài Hermes) và định
nghĩa class kế thừa BasePlatformAdapter, nên test nạp module sau khi bỏ hai
node đó khỏi AST — phần còn lại là code thật, không phải bản chép tay.
"""

import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_ADAPTER = os.path.join(_ROOT, "adapter.py")


def _load_adapter_without_gateway():
    with open(_ADAPTER, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    kept = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("gateway"):
            continue
        if isinstance(node, ast.ClassDef) and node.name == "ZaloOaAdapter":
            continue  # kế thừa BasePlatformAdapter → cần gateway lúc tạo class
        kept.append(node)
    module = ast.Module(body=kept, type_ignores=[])
    ns = {"__name__": "zalo_oa_adapter_test", "__file__": _ADAPTER}
    exec(compile(module, _ADAPTER, "exec"), ns)
    return ns


_NS = _load_adapter_without_gateway()
split_message = _NS["split_message"]


class SplitMessageTest(unittest.TestCase):
    def test_short_message_untouched(self):
        self.assertEqual(split_message("chào anh", 100), ["chào anh"])

    def test_splits_on_paragraph_boundary(self):
        text = "đoạn một" + " " * 0 + "\n\n" + "đoạn hai"
        chunks = split_message(text, 12)
        self.assertEqual(chunks[0], "đoạn một")

    def test_never_exceeds_limit(self):
        text = " ".join(f"từ{i}" for i in range(300))
        for chunk in split_message(text, 40):
            self.assertLessEqual(len(chunk), 40)

    def test_hard_cut_when_no_separator(self):
        chunks = split_message("x" * 250, 100)
        self.assertEqual([len(c) for c in chunks], [100, 100, 50])

    def test_no_empty_chunks(self):
        for chunk in split_message("a\n\n\n\n" + "b" * 300, 50):
            self.assertTrue(chunk.strip())

    def test_content_is_preserved(self):
        text = " ".join(f"từ{i}" for i in range(200))
        joined = " ".join(split_message(text, 37))
        self.assertEqual(joined.split(), text.split())


class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("ZALO_OA_SESSION_DIR")
        self.dir = tempfile.mkdtemp()
        os.environ["ZALO_OA_SESSION_DIR"] = self.dir

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("ZALO_OA_SESSION_DIR", None)
        else:
            os.environ["ZALO_OA_SESSION_DIR"] = self._prev

    def test_off_by_default(self):
        self.assertFalse(_NS["_get_maintenance"]().get("enabled"))

    def test_set_and_read_back(self):
        self.assertTrue(_NS["_set_maintenance"](True, "Bên em bảo trì tới 15h30 ạ"))
        self.assertEqual(_NS["_maintenance_message"](), "Bên em bảo trì tới 15h30 ạ")
        self.assertTrue(_NS["_get_maintenance"]()["enabled"])

    def test_default_message_when_no_custom(self):
        _NS["_set_maintenance"](True, "")
        self.assertEqual(_NS["_maintenance_message"](), _NS["_MAINT_DEFAULT_MSG"])

    def test_persona_overrides_default(self):
        Path(self.dir, "oa_persona.json").write_text(
            '{"notices": {"maintenance": "Dạ shop đang bảo trì, tí nữa em rep liền ạ"}}',
            encoding="utf-8",
        )
        _NS["_set_maintenance"](True, "")
        self.assertEqual(
            _NS["_maintenance_message"](), "Dạ shop đang bảo trì, tí nữa em rep liền ạ"
        )

    def test_custom_message_beats_persona(self):
        Path(self.dir, "oa_persona.json").write_text(
            '{"notices": {"maintenance": "câu persona"}}', encoding="utf-8"
        )
        _NS["_set_maintenance"](True, "câu owner đặt")
        self.assertEqual(_NS["_maintenance_message"](), "câu owner đặt")

    def test_corrupt_flag_file_fails_open(self):
        Path(self.dir, "maintenance.json").write_text("{oops", encoding="utf-8")
        self.assertFalse(_NS["_get_maintenance"]().get("enabled"))

    def test_default_message_is_not_personalised(self):
        low = _NS["_MAINT_DEFAULT_MSG"].lower()
        for name in ("ông bụt", "hermes", "gpt", "openai"):
            self.assertNotIn(name, low)


class EnvFlagTest(unittest.TestCase):
    def test_truthy_values(self):
        for v in ("1", "true", "TRUE", "yes", "on"):
            os.environ["ZALO_OA_TEST_FLAG"] = v
            self.assertTrue(_NS["_env_flag"]("ZALO_OA_TEST_FLAG"))

    def test_falsey_values(self):
        for v in ("0", "false", "no", "", "off"):
            os.environ["ZALO_OA_TEST_FLAG"] = v
            self.assertFalse(_NS["_env_flag"]("ZALO_OA_TEST_FLAG"))

    def test_paid_window_defaults_to_blocked(self):
        # Mặc định phải là CHẶN: bật nhầm là âm thầm mất tiền.
        os.environ.pop("ZALO_OA_ALLOW_PAID_WINDOW", None)
        self.assertFalse(_NS["_env_flag"]("ZALO_OA_ALLOW_PAID_WINDOW"))

    def tearDown(self):
        os.environ.pop("ZALO_OA_TEST_FLAG", None)


class RegistrationTest(unittest.TestCase):
    def test_check_requirements_needs_all_four_secrets(self):
        keys = [
            "ZALO_OA_APP_ID",
            "ZALO_OA_APP_SECRET",
            "ZALO_OA_SECRET_KEY",
            "ZALO_OA_PUBLIC_BASE_URL",
        ]
        saved = {k: os.environ.get(k) for k in keys}
        try:
            for k in keys:
                os.environ[k] = "x"
            self.assertTrue(_NS["check_requirements"]())
            for missing in keys:
                os.environ.pop(missing)
                self.assertFalse(_NS["check_requirements"](), missing)
                os.environ[missing] = "x"
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_register_passes_platform_metadata(self):
        captured = {}

        class Ctx:
            def register_platform(self, **kwargs):
                captured.update(kwargs)

        _NS["register"](Ctx())
        self.assertEqual(captured["name"], "zalo-oa")
        self.assertIn("ZALO_OA_SECRET_KEY", captured["required_env"])
        self.assertEqual(captured["cron_deliver_env_var"], "ZALO_OA_HOME_CHANNEL")

    def test_register_survives_older_hermes_without_cron_kwarg(self):
        captured = {}

        class OldCtx:
            def register_platform(self, **kwargs):
                if "cron_deliver_env_var" in kwargs:
                    raise TypeError("unexpected keyword argument")
                captured.update(kwargs)

        _NS["register"](OldCtx())
        self.assertEqual(captured["name"], "zalo-oa")


if __name__ == "__main__":
    unittest.main()
