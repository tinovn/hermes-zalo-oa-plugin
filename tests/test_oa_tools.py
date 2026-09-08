"""Tool gửi file/ảnh qua Open API của OA (``oa_tools``).

Trọng tâm là những chỗ đã hỏng thật hoặc sẽ hỏng âm thầm: bắc cầu sang loop
của gateway, tìm người nhận từ task_id, và các cửa chặn (URL, file thiếu,
file quá cỡ) — vì mọi lỗi ở đây đều biểu hiện giống nhau với người dùng cuối:
khách không nhận được gì.
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import oa_tools  # noqa: E402


class _Result:
    def __init__(self, success=True, message_id="m1", error=None):
        self.success = success
        self.message_id = message_id
        self.error = error


class _FakeAdapter:
    """Adapter giả có event loop chạy nền, giống hệt cảnh thật: tool gọi từ
    thread khác, adapter sống trên loop của gateway."""

    def __init__(self, result=None):
        self.calls = []
        self._result = result or _Result()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    async def _send_attachment(self, chat_id, path, caption, force_file=False, override_name=None):
        self.calls.append(
            {
                "chat_id": chat_id,
                "path": str(path),
                "caption": caption,
                "force_file": force_file,
                "override_name": override_name,
                # Ghi lại loop thực thi để test chứng minh được là đã chạy
                # đúng trên loop của adapter, không phải loop tạm nào khác.
                "loop": asyncio.get_running_loop(),
            }
        )
        return self._result

    def stop(self):
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()


def _call(fn, *a, **kw):
    """Handler trả CHUỖI JSON (đúng thứ provider cần). Test parse ra dict để
    kiểm tra nội dung, nhưng kiểu trả về được khoá riêng ở ResultShapeTest."""
    out = fn(*a, **kw)
    assert isinstance(out, str), f"handler phai tra chuoi, dang tra {type(out).__name__}"
    return json.loads(out)


class ToolBaseTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.f = self.dir / "hopdong.pdf"
        self.f.write_bytes(b"%PDF-1.4\n%%EOF\n")
        self.adapter = _FakeAdapter()
        oa_tools.set_live_adapter(self.adapter)

    def tearDown(self):
        oa_tools.clear_live_adapter()
        self.adapter.stop()


class SendFileTest(ToolBaseTest):
    def test_sends_on_the_adapter_loop_not_a_temp_one(self):
        out = _call(oa_tools.handle_send_file, 
            {"file_path": str(self.f), "user_id": "u1", "caption": "hợp đồng"}
        )
        self.assertTrue(out["success"], out)
        self.assertEqual(out["chat_id"], "u1")
        self.assertEqual(len(self.adapter.calls), 1)
        call = self.adapter.calls[0]
        self.assertIs(
            call["loop"], self.adapter._loop,
            "phải chạy trên loop của adapter, nếu không khoá chống refresh song song vô tác dụng",
        )
        self.assertEqual(call["caption"], "hợp đồng")

    def test_file_forces_document_and_image_does_not(self):
        _call(oa_tools.handle_send_file, {"file_path": str(self.f), "user_id": "u1"})
        _call(oa_tools.handle_send_image, {"file_path": str(self.f), "user_id": "u1"})
        self.assertTrue(self.adapter.calls[0]["force_file"])
        self.assertFalse(self.adapter.calls[1]["force_file"])

    def test_url_is_refused_to_avoid_ssrf(self):
        out = _call(oa_tools.handle_send_file, 
            {"url": "http://169.254.169.254/latest/meta-data/", "user_id": "u1"}
        )
        self.assertFalse(out["success"])
        self.assertIn("URL", out["error"])
        self.assertEqual(self.adapter.calls, [], "không được gọi tới adapter")

    def test_missing_file_reported_clearly(self):
        out = _call(oa_tools.handle_send_file, {"file_path": str(self.dir / "khong-co.pdf"), "user_id": "u1"})
        self.assertFalse(out["success"])
        self.assertIn("không tồn tại", out["error"])

    def test_empty_file_refused(self):
        empty = self.dir / "rong.pdf"
        empty.write_bytes(b"")
        out = _call(oa_tools.handle_send_file, {"file_path": str(empty), "user_id": "u1"})
        self.assertFalse(out["success"])
        self.assertIn("rỗng", out["error"])

    def test_oversized_refused_before_upload(self):
        big = self.dir / "to.pdf"
        big.write_bytes(b"x" * (oa_tools.SEND_FILE_MAX_BYTES + 1))
        out = _call(oa_tools.handle_send_file, {"file_path": str(big), "user_id": "u1"})
        self.assertFalse(out["success"])
        self.assertIn("vượt trần", out["error"])
        self.assertEqual(self.adapter.calls, [], "phải chặn TRƯỚC khi upload")

    def test_missing_file_path_refused(self):
        out = _call(oa_tools.handle_send_file, {"user_id": "u1"})
        self.assertFalse(out["success"])
        self.assertIn("file_path", out["error"])

    def test_failure_from_adapter_is_surfaced(self):
        self.adapter._result = _Result(success=False, error="[-201] file is invalid")
        out = _call(oa_tools.handle_send_file, {"file_path": str(self.f), "user_id": "u1"})
        self.assertFalse(out["success"])
        self.assertIn("-201", out["error"])

    def test_no_live_adapter_gives_clear_error(self):
        oa_tools.clear_live_adapter()
        out = _call(oa_tools.handle_send_file, {"file_path": str(self.f), "user_id": "u1"})
        self.assertFalse(out["success"])
        self.assertIn("chưa kết nối", out["error"])


class ArgShapeTest(ToolBaseTest):
    """Model gói tham số đủ kiểu; handler phải đỡ được hết."""

    def test_args_as_json_string(self):
        out = _call(oa_tools.handle_send_file, json.dumps({"file_path": str(self.f), "user_id": "u1"}))
        self.assertTrue(out["success"], out)

    def test_args_spread_into_kwargs(self):
        out = _call(oa_tools.handle_send_file, None, file_path=str(self.f), user_id="u1")
        self.assertTrue(out["success"], out)

    def test_string_wrapped_in_object(self):
        out = _call(oa_tools.handle_send_file, {"file_path": {"value": str(self.f)}, "user_id": "u1"})
        self.assertTrue(out["success"], out)

    def test_chat_id_accepted_as_alias_of_user_id(self):
        out = _call(oa_tools.handle_send_file, {"file_path": str(self.f), "chat_id": "u9"})
        self.assertTrue(out["success"], out)
        self.assertEqual(out["chat_id"], "u9")

    def test_filename_override_passed_through(self):
        _call(oa_tools.handle_send_file, 
            {"file_path": str(self.f), "user_id": "u1", "filename": "Hop dong.pdf"}
        )
        self.assertEqual(self.adapter.calls[0]["override_name"], "Hop dong.pdf")


class ResolveRecipientTest(ToolBaseTest):
    """Agent hay quên user_id — phải suy ra từ task_id qua sessions.json."""

    def _write_sessions(self, payload):
        home = self.dir / "hermes_home"
        (home / "sessions").mkdir(parents=True, exist_ok=True)
        (home / "sessions" / "sessions.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        os.environ["HERMES_HOME"] = str(home)
        self.addCleanup(os.environ.pop, "HERMES_HOME", None)

    def test_resolves_from_task_id(self):
        self._write_sessions(
            {
                "s1": {
                    "platform": "zalo-oa",
                    "session_id": "task-abc",
                    "origin": {"chat_id": "khach-123"},
                }
            }
        )
        out = _call(oa_tools.handle_send_file, {"file_path": str(self.f), "task_id": "task-abc"})
        self.assertTrue(out["success"], out)
        self.assertEqual(out["chat_id"], "khach-123")

    def test_does_not_borrow_chat_id_from_another_platform(self):
        # Cùng session_id nhưng của kênh cá nhân — lấy nhầm là gửi tin sang
        # tài khoản khác, phải từ chối.
        self._write_sessions(
            {
                "s1": {
                    "platform": "zalo-personal",
                    "session_id": "task-abc",
                    "origin": {"chat_id": "nguoi-la"},
                }
            }
        )
        out = _call(oa_tools.handle_send_file, {"file_path": str(self.f), "task_id": "task-abc"})
        self.assertFalse(out["success"])
        self.assertIn("người nhận", out["error"])

    def test_survives_sentinel_entries(self):
        self._write_sessions(
            {
                "_README": "day khong phai session",
                "s1": {
                    "platform": "zalo-oa",
                    "session_id": "task-abc",
                    "origin": {"chat_id": "khach-123"},
                },
            }
        )
        out = _call(oa_tools.handle_send_file, {"file_path": str(self.f), "task_id": "task-abc"})
        self.assertTrue(out["success"], out)

    def test_missing_sessions_file_is_not_fatal(self):
        os.environ["HERMES_HOME"] = str(self.dir / "khong-ton-tai")
        self.addCleanup(os.environ.pop, "HERMES_HOME", None)
        self.assertEqual(oa_tools.resolve_chat_id_from_task("task-abc"), "")


class ResultShapeTest(ToolBaseTest):
    """Tin role="tool" phải có content là CHUỖI. Trả dict thô làm DeepSeek đáp
    HTTP 400 và giết vĩnh viễn cả phiên chat — đã xảy ra trên production."""

    def test_handlers_return_json_string_not_dict(self):
        for fn in (oa_tools.handle_send_file, oa_tools.handle_send_image):
            out = fn({"file_path": str(self.f), "user_id": "u1"})
            self.assertIsInstance(out, str, f"{fn.__name__} phai tra chuoi")
            self.assertIsInstance(json.loads(out), dict, "chuoi phai parse ra JSON hop le")

    def test_error_path_also_returns_string(self):
        out = oa_tools.handle_send_file({"user_id": "u1"})  # thieu file_path
        self.assertIsInstance(out, str)
        self.assertFalse(json.loads(out)["success"])


class RegisterToolsTest(unittest.TestCase):
    def test_registers_both_tools_in_the_oa_toolset(self):
        seen = []

        class Ctx:
            def register_tool(self, **kw):
                seen.append(kw)

        oa_tools.register_tools(Ctx())
        names = [k["name"] for k in seen]
        self.assertEqual(names, ["oa_send_file", "oa_send_image"])
        # Tên KHÔNG được bắt đầu bằng "zalo_": hook pre_tool_call của plugin
        # Zalo cá nhân chặn mọi tool có tiền tố đó khi phiên không phải
        # zalo-personal. Đã gây loop vô hạn trên production một lần.
        for n in names:
            self.assertFalse(n.startswith("zalo_"), f"{n} se bi hook zalo-personal chan")
        for k in seen:
            self.assertEqual(k["toolset"], "hermes-zalo-oa")
            self.assertIn("file_path", k["schema"]["parameters"]["properties"])
            self.assertEqual(k["schema"]["parameters"]["required"], ["file_path"])

    def test_schema_warns_the_model_off_personal_tools_and_docx(self):
        # Hàng rào mềm nhưng là thứ duy nhất ngăn model chọn lại zalo_send_file
        # khi toolset chưa bị thu hẹp.
        desc = oa_tools.SEND_FILE_SCHEMA["description"]
        self.assertIn("zalo_send_file", desc)
        self.assertIn("PDF", desc)

    def test_registration_failure_does_not_kill_the_plugin(self):
        class BrokenCtx:
            def register_tool(self, **kw):
                raise RuntimeError("phien ban Hermes khong ho tro")

        oa_tools.register_tools(BrokenCtx())  # không được ném ra ngoài


if __name__ == "__main__":
    unittest.main()
