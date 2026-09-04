"""Sổ theo dõi cửa sổ tin Tư vấn của Zalo OA.

Vì sao phải có: OA KHÔNG được nhắn tự do như tài khoản cá nhân.

  * Trong **48 giờ** kể từ tương tác cuối của người dùng: tin Tư vấn miễn phí.
  * Ngoài 48 giờ nhưng còn trong **7 ngày**: OpenAPI vẫn gửi được nhưng
    **Zalo tính phí** theo bảng giá.
  * Quá 7 ngày: OpenAPI không gửi được nữa (API trả -230/-232).

(Nguồn: tài liệu vận hành OA tại oa.zalo.me, bản hiệu lực 01/01/2026.)

Nếu không có sổ này thì mỗi lần cron/nhắc lịch bắn tin là một lần đốt tiền âm
thầm, và không ai biết cho tới lúc nhận hoá đơn. Nên mặc định plugin CHẶN mọi
tin ngoài khung 48h; muốn trả tiền thì bật ``ZALO_OA_ALLOW_PAID_WINDOW=true``.

Trạng thái "chưa từng thấy tương tác" cũng bị chặn: tin trả lời khách luôn đi
ngay sau một webhook inbound (đã ghi nhận tương tác), nên hồ sơ trống gần như
chỉ xảy ra với tin CHỦ ĐỘNG gửi cho người chưa từng nhắn — đúng ca rủi ro nhất.

Module thuần: không import Hermes, không đọc env, thời gian truyền vào từ
ngoài để test được.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

FREE_WINDOW_HOURS = 48.0
API_WINDOW_DAYS = 7.0
# Dọn hồ sơ cũ hơn mốc này (đằng nào cũng quá 7 ngày = không gửi được).
_PRUNE_AFTER_S = 30 * 86400.0


@dataclass
class WindowDecision:
    allowed: bool
    within_free: bool     # trong 48h → miễn phí
    within_api: bool      # trong 7 ngày → API còn gửi được
    seconds_since: Optional[float]
    reason: str


class ConsultationWindow:
    """Ghi nhận tương tác cuối theo từng user và quyết định có được gửi không."""

    def __init__(self, path: Path, allow_paid_window: bool = False):
        self.path = Path(path)
        self.allow_paid_window = bool(allow_paid_window)
        self._state: Dict[str, Dict[str, Any]] = self._load()

    # ── đĩa ──
    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    return {str(k): v for k, v in d.items() if isinstance(v, dict)}
        except Exception as e:
            logger.warning(f"[zalo-oa] đọc sổ cửa sổ tư vấn lỗi ({e}) — bắt đầu lại từ rỗng")
        return {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self.path)
        except Exception as e:
            logger.warning(f"[zalo-oa] ghi sổ cửa sổ tư vấn lỗi: {e}")

    def _prune(self, now: float) -> None:
        if len(self._state) <= 1000:
            return
        self._state = {
            k: v
            for k, v in self._state.items()
            if now - float(v.get("last_inbound") or 0) <= _PRUNE_AFTER_S
        }

    # ── ghi nhận ──
    def mark_inbound(self, user_id: str, now: Optional[float] = None) -> None:
        """Người dùng vừa tương tác → mở lại cửa sổ 48h và reset bộ đếm."""
        now = time.time() if now is None else now
        self._prune(now)
        self._state[str(user_id)] = {"last_inbound": now, "sent": 0}
        self._save()

    def note_outbound(self, user_id: str) -> int:
        rec = self._state.setdefault(str(user_id), {"last_inbound": 0.0, "sent": 0})
        rec["sent"] = int(rec.get("sent") or 0) + 1
        self._save()
        return int(rec["sent"])

    def sent_count(self, user_id: str) -> int:
        return int((self._state.get(str(user_id)) or {}).get("sent") or 0)

    def last_inbound_at(self, user_id: str) -> Optional[float]:
        ts = (self._state.get(str(user_id)) or {}).get("last_inbound")
        return float(ts) if ts else None

    # ── quyết định ──
    def evaluate(self, user_id: str, now: Optional[float] = None) -> WindowDecision:
        now = time.time() if now is None else now
        last = self.last_inbound_at(user_id)
        if not last:
            return WindowDecision(
                allowed=False, within_free=False, within_api=False, seconds_since=None,
                reason="chưa ghi nhận tương tác nào từ người dùng này",
            )
        elapsed = now - last
        within_free = elapsed <= FREE_WINDOW_HOURS * 3600
        within_api = elapsed <= API_WINDOW_DAYS * 86400
        if within_free:
            return WindowDecision(True, True, True, elapsed, "trong khung 48h miễn phí")
        if not within_api:
            return WindowDecision(
                False, False, False, elapsed,
                f"quá 7 ngày kể từ tương tác cuối ({elapsed / 86400:.1f} ngày) — OpenAPI không gửi được",
            )
        if self.allow_paid_window:
            return WindowDecision(
                True, False, True, elapsed,
                f"ngoài 48h ({elapsed / 3600:.1f}h) — Zalo TÍNH PHÍ tin này",
            )
        return WindowDecision(
            False, False, True, elapsed,
            f"ngoài 48h ({elapsed / 3600:.1f}h) — bị chặn vì tin ngoài khung mất phí "
            "(bật ZALO_OA_ALLOW_PAID_WINDOW=true nếu chấp nhận trả phí)",
        )
