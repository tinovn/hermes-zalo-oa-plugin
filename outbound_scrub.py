"""Lọc nội dung ĐI RA cho khách: chặn rò rỉ vận hành, chuyển markdown → plain.

Port nguyên bộ regex từ plugin `zalo-personal` (đã chạy production ở đó). Với
kênh OA còn quan trọng hơn: đây là kênh CHÍNH THỨC mang thương hiệu doanh
nghiệp — lọt một dòng "retrying in 3s" hay "Hermes/GPT" ra khách là mất mặt
thật, không phải chuyện nội bộ.

``message_filtering.classify`` bắt các nhóm lifecycle/terminal có cấu trúc;
module này bắt phần còn lại theo mặt chữ: câu trạng thái của runtime, tin mở
đầu bằng emoji hệ thống, văn bản "planning" tiếng Anh model lỡ xuất ra, và
markdown mà Zalo không render.

Module thuần: chỉ đọc ZALO_OA_OWNER_NAME (tuỳ chọn) để che tên thật của chủ.
"""

import os
import re
from typing import Optional


# Hermes lifecycle notifications (retries, provider hiccups, home-channel
# prompts, etc.) — they leak implementation details and confuse end-users.
_NOISY_STATUS_RE = re.compile(
    r"("
    r"retrying\s+in\s+\d"
    r"|max\s+retries\s+\(\d+\)"
    r"|stream\s+drop"
    r"|no\s+first\s+byte"
    r"|no\s+response\s+from\s+provider"           # non-streaming timeout
    r"|aborting\s+call"                            # final timeout
    r"|reconnecting"                               # reconnect attempts (with or without trailing dots)
    r"|rate\s+limited"
    r"|stale\s+connections"
    r"|preflight\s+compression"
    r"|fallback\s+context\s+marker"
    r"|compression\s+summary\s+failed"
    r"|auxiliary\s+.+\s+failed"
    r"|no\s+auxiliary\s+llm\s+provider"
    r"|auto-lowered\s+compression"
    r"|auto-?compaction\s+was\s+raised"
    r"|caps\s+context\s+at"
    r"|compression\.\w*autoraise"
    r"|compression\s+aborted"
    r"|conversation\s+continues\s+unchanged"
    r"|no\s+messages\s+were\s+dropped"
    r"|start\s+a\s+fresh\s+session"
    r"|run\s+/compress"
    r"|/compress\s+to\s+retry"
    r"|/new\s+to\s+start"
    r"|total\s+timeout"
    r"|auxiliary\s+\w+\s+stream"
    r"|session\s+(?:was\s+)?automatically\s+reset"
    r"|invalid\s+responses"
    r"|trying\s+fallback"
    r"|home\s+channel\s+is\s+set"
    r"|/sethome"
    r"|/hermes\s+sethome"
    r"|codex\s+stream"
    r"|non[-\s]?streaming"
    r"|provider\s+(?:error|hiccup|timeout)"
    r"|api\s+(?:call\s+)?failed"
    r"|api(?:connection)?error"
    r"|backend\s+accepted\s+the\s+connection"
    r"|killing\s+connection"
    r"|streaming\s+disabled"
    r"|connection\s+error"
    r"|chunk\s+timeout"
    r"|ttfb\s+(?:timeout|cutoff)"
    r"|context[\s-]?pressure"
    r"|model:\s*[\w\.-]+"                          # "model: gpt-5.3-codex" or "model: trợ lý"
    r"|still\s+working"                            # "Still working... (X min elapsed)"
    r"|min\s+elapsed"                              # "(3 min elapsed —..."
    r"|iteration\s+\d+\s*/\s*\d+"                  # "iteration 2/60"
    r"|running:\s*[\w_-]+"                         # "running: image_generate"
    r"|tool\s+\w+\s+returned\s+error"
    r"|attempting\s+to\s+(?:reconnect|retry)"
    r"|elapsed\s*[—\-]\s*iteration"
    r"|hermes_plugins?\."                          # internal plugin namespace leak
    r"|gateway\.run:"
    r"|self[\-\s]?improvement\s+review"             # 💾 Self-improvement review
    r"|user\s+profile\s+updated"
    r"|memory\s+(?:store|updated|saved|review)"
    r"|honcho\s+"                                   # memory backend leak
    r"|skill\s+(?:loaded|registered)"
    r"|compacting\s+context"                        # 🗜️ Compacting context — summarizing...
    r"|summariz(?:e|es|ing)\s+earlier\s+conversation"
    r"|so\s+i\s+can\s+continue"                      # đuôi câu thông báo nén
    r")",
    re.IGNORECASE,
)

# Generic safeguard: if a message starts with a warning/clock emoji AND
# carries a technical token (model/provider/stream/retry/api), drop it
# even if no specific phrase matched. Catches future variants without
# requiring a regex update each time.
_STATUS_EMOJI_PREFIX_RE = re.compile(
    r"^\s*(?:⚠|ℹ️|ℹ|⚠️|⏳|⏱️|⏱|📬|🔄|🔁|❌|⛔|🛑|💥|💾|📝|🧠|🗒️|📋|🔧|⚙️|🔍|🗜️|🗜|📦|💤|⟳|↻|✓|✔️|✔|☑️|☑)"
)
_STATUS_TOKEN_RE = re.compile(
    r"\b(model|provider|stream|streaming|retry|retrying|api|connection|"
    r"timeout|reconnect|backend|chunk|ttfb|abort|fallback)\b",
    re.IGNORECASE,
)

# Brand / implementation names that must not leak to end users.
_BRAND_REDACT_RE = re.compile(
    r"(?i)(hermes(?:[\s-]agent)?|codex|gpt-?5(?:\.\d+)?(?:-codex)?|gpt-?4[a-z\.\d-]*|openai|anthropic|claude\s+\d?(?:\.\d+)?(?:\s*(?:sonnet|opus|haiku))?)"
)


# Prompt-injection patterns: phrases users use to try to override the
# system prompt. These don't need to be perfect — anything we catch
# gets wrapped so the LLM sees the user's text as untrusted DATA, not
# as system instructions.
_PROMPT_INJECTION_RE = re.compile(
    r"("
    r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions?|prompts?|rules?)"
    r"|disregard\s+(?:all\s+)?(?:previous|prior|above)"
    r"|forget\s+(?:all\s+)?(?:previous|prior|your\s+instructions)"
    r"|you\s+are\s+now\s+(?:a|an)\s+"
    r"|pretend\s+(?:you\s+are|to\s+be)\s+"
    r"|act\s+as\s+(?:a|an|if)\s+"
    r"|new\s+instructions?:"
    r"|system\s*:\s*"
    r"|<\s*(?:system|admin|root|developer)\s*>"
    r"|\[\s*(?:system|admin|root|developer)\s*\]"
    r"|\bjail\s*break\b"
    r"|reveal\s+(?:your\s+)?(?:system\s+)?prompt"
    r"|show\s+(?:me\s+)?(?:your|the)\s+(?:system\s+)?(?:prompt|instructions?)"
    r"|b[oỏ]?\s*qua\s+(?:mọi\s+)?(?:chỉ\s+dẫn|hướng\s+dẫn|quy\s+tắc)"  # "bỏ qua mọi chỉ dẫn"
    r"|quên\s+(?:mọi\s+)?(?:chỉ\s+dẫn|hướng\s+dẫn|quy\s+tắc)"
    r"|giả\s+vờ\s+(?:làm|là)\s+"
    r"|em\s+không\s+phải\s+bot"
    r"|hãy\s+làm\s+như\s+thể\s+(?:em\s+)?là"
    r")",
    re.IGNORECASE,
)


# Non-owner: tin MỞ ĐẦU bằng emoji/ký hiệu → luôn là thông báo Hermes
# (⚠/ℹ/◐/🔄/💾/🗜...). Reply thật của bot (persona) mở đầu bằng chữ, không emoji.
_LEADING_EMOJI_RE = re.compile(
    r"^\s*(?:"
    r"⚠|ℹ|◐|◑|◒|◓|◆|◇|⏳|⌛|⏱|📬|🔄|🔁|⟳|↻|❌|⛔|🛑|💥|💾|🗜|🧠|🔧|⚙|🔍|📝|🗒|📋|📦|💤"
    r")️?"
)

# Hermes lifecycle glyphs that a REAL Vietnamese reply also plausibly opens
# with ("✔ Đã đặt lịch cho anh", "⚡ Đơn đang giao"). Dropping on the glyph
# alone would eat legitimate answers, so these only count as a system notice
# when the text carries no Vietnamese diacritic — i.e. it is raw English
# runtime output such as "✓ Context compaction complete — continuing turn...".
_LEADING_EMOJI_EN_ONLY_RE = re.compile(r"^\s*(?:✓|✔|☑|⚡)️?")


# Reply thật của bot LUÔN tiếng Việt (ta/con). Đôi khi model lỡ xuất văn bản
# PLANNING tiếng Anh ("We need... Let's call get latest to inspect...") ra chat.
_VN_DIACRITIC_RE = re.compile(
    r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ]",
    re.IGNORECASE,
)
_MODEL_PLANNING_RE = re.compile(
    r"(?i)(?:let'?s\s+\w+|let\s+me\s+\w+|we\s+(?:need|should|must|can|have\s+to)"
    r"|i'?ll\s+\w+|i\s+need\s+to|i\s+should|call\s+get|get\s+latest|update\s+fields"
    r"|maybe\s+enough|possibly\s+forbidden|to\s+inspect|first\s+and\s+html)"
)


def _scrub_outgoing(text: str) -> Optional[str]:
    """Return cleaned text safe for end-user delivery, or None to drop.

    Rules (any-match → drop):
    1. Specific noisy-status phrases (retry/timeout/sethome/...)
    2. Generic: starts with status emoji (⚠️/⏳/📬/🔄) AND contains a
       technical token (model/provider/stream/retry/api/...) — catches
       new variants of provider-status warnings without needing a regex
       update each time.

    Otherwise apply brand redaction so "Hermes/Codex/GPT-5/OpenAI" don't
    leak in legitimate replies.
    """
    if not text:
        return None
    t = text.strip()
    if not t:
        return None
    if _NOISY_STATUS_RE.search(t):
        return None
    if _STATUS_EMOJI_PREFIX_RE.match(t) and _STATUS_TOKEN_RE.search(t):
        return None
    # Rule 3: mở đầu bằng emoji/ký hiệu → thông báo Hermes, drop cho khách.
    if _LEADING_EMOJI_RE.match(t):
        return None
    # Rule 3b: glyph dùng chung với câu trả lời thật ("✔ Đã đặt lịch") — chỉ
    # drop khi KHÔNG có dấu tiếng Việt, tức là text vận hành tiếng Anh.
    if _LEADING_EMOJI_EN_ONLY_RE.match(t) and not _VN_DIACRITIC_RE.search(t):
        return None
    # Rule 4: model reasoning/planning tiếng Anh lọt ra (không có tiếng Việt +
    # dính >=2 mẫu planning). Reply thật luôn tiếng Việt → an toàn.
    if not _VN_DIACRITIC_RE.search(t) and len(_MODEL_PLANNING_RE.findall(t)) >= 2:
        return None
    # Mild brand redaction. Keep the message structure but swap names.
    t = _BRAND_REDACT_RE.sub("trợ lý", t)
    # Che tên chủ tài khoản (TÙY CHỌN): nếu khai báo ZALO_OWNER_NAME, thay
    # mọi biến thể tên đó bằng cách xưng hô (ZALO_OWNER_NICKNAME, mặc định
    # "sếp"). Mặc định KHÔNG khai báo → không che gì.
    if _OWNER_NAME_REDACT_RE is not None:
        t = _OWNER_NAME_REDACT_RE.sub(_OWNER_NICKNAME, t)
    return t


# ---------------------------------------------------------------------------
# Cron / reminder delivery envelope stripper.
#
# When a Hermes cron/reminder job FIRES, core delivers the body wrapped in a
# technical envelope, e.g.:
#
#     Cronjob Response: Nhắc đánh pick
#     (job_id: 90d5be72fd33)
#     -------------
#
#     Anh ơi tới giờ đánh pick rồi nha 🏓
#
#     To stop or manage this job, send me a new message (e.g. "stop reminder Nhắc đánh pick").
#
# End users should only ever see the natural-language body. We strip the header
# (everything up to and including the dashed separator) and the management
# footer. Applied to ALL outbound text — the home channel is the owner DM, which
# send() otherwise passes through unscrubbed, so this is the only place the
# envelope gets cleaned.
# ---------------------------------------------------------------------------
_CRON_HEADER_RE = re.compile(
    r"^\s*Cron(?:job)?\s+Response\s*:.*?\n\s*-{3,}\s*\n",
    re.IGNORECASE | re.DOTALL,
)
_CRON_FOOTER_RE = re.compile(
    r"\n+\s*To stop or manage this job\b.*$",
    re.IGNORECASE | re.DOTALL,
)


# ── Markdown → plaintext cho Zalo (Zalo KHÔNG render markdown) ──────────────
# Bot có thể trả lời kèm **bold**, #heading, `code`, [text](url)... Zalo hiện
# thô các dấu này. Chuyển về text thuần, GIỮ nội dung + link bấm được.
_MD_FENCE_RE   = re.compile(r"```[^\n]*\n?(.*?)```", re.DOTALL)
_MD_LINK_RE    = re.compile(r"(!)?\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_MD_BOLD_RE    = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)
_MD_STRIKE_RE  = re.compile(r"~~(.+?)~~", re.DOTALL)
_MD_ITALIC_RE  = re.compile(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\*)"
                            r"|(?<![_\w])_(?!\s)(.+?)(?<!\s)_(?!\w)")
_MD_CODE_RE    = re.compile(r"`([^`]+)`")
_MD_HR_RE      = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$", re.MULTILINE)
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_MD_QUOTE_RE   = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_MD_BULLET_RE  = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)


def _zalo_plaintext(text: str) -> str:
    """Chuyển markdown về text thuần cho Zalo. No-op nếu không có ký tự markdown."""
    if not text or not any(c in text for c in "*_`#[~>"):
        return text
    t = _MD_FENCE_RE.sub(lambda m: m.group(1), text)
    def _link(m):
        label, url = m.group(2), m.group(3)
        if m.group(1):
            return url
        return f"{label} ({url})" if label and label != url else url
    t = _MD_LINK_RE.sub(_link, t)
    t = _MD_BOLD_RE.sub(lambda m: m.group(1) or m.group(2), t)
    t = _MD_STRIKE_RE.sub(lambda m: m.group(1), t)
    t = _MD_ITALIC_RE.sub(lambda m: m.group(1) or m.group(2), t)
    t = _MD_CODE_RE.sub(lambda m: m.group(1), t)
    t = _MD_HR_RE.sub("", t)
    t = _MD_HEADING_RE.sub("", t)
    t = _MD_QUOTE_RE.sub("", t)
    t = _MD_BULLET_RE.sub(lambda m: f"{m.group(1)}• ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()




_FILE_MUTATION_FOOTER_RE = re.compile(
    r"\n*⚠️\s*File-mutation verifier:.*?(?=\n\n\S|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_SELF_IMPROVEMENT_REVIEW_RE = re.compile(
    r"^\s*💾\s*Self-improvement review\s*:.*$",
    re.IGNORECASE | re.DOTALL,
)
# Thông báo 1 lần của core khi gpt-5.5/Codex nâng ngưỡng auto-compaction
# ("ℹ ... caps context at 272K, so auto-compaction was raised to 85% ...
# Opt back out: hermes config set compression..."). Thuần thông tin vận
# hành — KHÔNG được để lọt ra người dùng Zalo. Strip cả khối notice; nếu
# tin chỉ gồm notice → rỗng → send() drop im lặng (owner DM vẫn giữ).
_COMPRESSION_AUTORAISE_RE = re.compile(
    r"(?:^|\n)[ \t]*ℹ?[^\n]*"
    r"(?:caps context at|auto[- ]?compaction was raised)"
    r"[^\n]*(?:\n[ \t]*[^\n]+)*",
    re.IGNORECASE,
)


# Dòng đuôi "Opt back out: hermes config set ..." của thông báo auto-compaction
# hay bị TÁCH khỏi dòng đầu bởi 1 dòng trống → _COMPRESSION_AUTORAISE_RE bỏ
# sót. Bắt riêng để strip (thuần vận hành, phải drop im lặng cho khách).
_COMPRESSION_OPTOUT_RE = re.compile(
    r"(?im)^[ \t]*Opt back out:[^\n]*hermes\s+config\s+set[^\n]*$"
)


def _strip_non_owner_internal_noise(text: str) -> str:
    """Remove owner/debug-only Hermes diagnostics from non-owner Zalo replies.

    Owner DM remains untouched. For normal users/groups we keep the natural
    assistant answer, but strip internal footers like the file-mutation verifier.
    Standalone background-review notices are dropped silently instead of being
    converted into a scary technical/error message.
    """
    if not text:
        return text
    t = text.strip()
    if _SELF_IMPROVEMENT_REVIEW_RE.match(t):
        return ""
    t = _FILE_MUTATION_FOOTER_RE.sub("", text)
    t = _COMPRESSION_AUTORAISE_RE.sub("", t).strip()
    t = _COMPRESSION_OPTOUT_RE.sub("", t).strip()
    return t


def _build_owner_name_re(name: str):
    """Tạo regex che tên chủ từ tên khai báo. None nếu không khai báo."""
    parts = [re.escape(p) for p in (name or "").split() if p]
    if not parts:
        return None
    full = r"\s+".join(parts)
    return re.compile(
        r"(?:(?:anh|chị|sếp|giám\s+đốc|sep|giam\s+doc)\s+)?" + full,
        re.IGNORECASE,
    )


# Tên thật của chủ cần che khi bot lỡ nhắc; để trống = không che gì.
_OWNER_NAME = (os.getenv("ZALO_OA_OWNER_NAME") or os.getenv("ZALO_OWNER_NAME") or "").strip()
_OWNER_NICKNAME = (
    os.getenv("ZALO_OA_OWNER_NICKNAME") or os.getenv("ZALO_OWNER_NICKNAME") or "sếp"
).strip()
_OWNER_NAME_REDACT_RE = _build_owner_name_re(_OWNER_NAME)


# API công khai. Tên nội bộ giữ nguyên như plugin `zalo-personal` để sau này
# còn diff/đồng bộ được khi bên đó sửa regex.
scrub_outgoing = _scrub_outgoing
zalo_plaintext = _zalo_plaintext
strip_internal_noise = _strip_non_owner_internal_noise
