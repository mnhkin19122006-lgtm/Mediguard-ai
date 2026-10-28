# ==============================================================================
# MEDIGUARD AI - COMPREHENSIVE MULTI-AGENT LLM CONVERSATION SERVICE FOR ELDERLY CARE
# 3 Autonomous Voice Agents with Distinct Personalities, Reasoning & Demeanors:
# 1. Cháu Gái Hoài My (vi-VN-HoaiMyNeural): Dịu dàng, nết na, thủ thỉ tâm tình, ấm áp
# 2. Cháu Trai Nam Minh (vi-VN-NamMinhNeural): Trầm ấm, đĩnh đạc, vững vàng, kỷ luật
# 3. Trợ Lý Thông Thái (vi-VN-Google): Nhanh nhẹn, thông thái, mạch lạc y khoa
# ==============================================================================

import os
import json
import time
import logging
import asyncio
import urllib.request
import urllib.error
import re
from datetime import datetime
from app.core.datetime_utils import utc_now, utc_now_iso
from typing import Optional, Dict, Any, List
from pathlib import Path
from dotenv import load_dotenv

from app.config import STORAGE_DIR, settings

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
ROOT_DIR = BASE_DIR.parent

SESSIONS_FILE = STORAGE_DIR / "companion_sessions.json"

SEARCH_KEYWORDS = [
    "giá vàng", "chứng khoán", "tỷ giá", "bóng đá", "kết quả xổ số", "tin thời sự",
    "thời sự", "tin tức", "tin mới", "tìm trên mạng", "tra google", "tra cứu trên mạng",
    "trên internet", "tin nóng", "thế giới có gì mới", "trong nước có gì mới",
    "tin tức mới nhất", "báo chí", "tra cứu internet"
]


def detect_search_need(prompt: str) -> bool:
    """Xác định câu hỏi có yêu cầu thông tin mới/tìm kiếm Internet hay không."""
    if not prompt:
        return False
    lower = prompt.lower()
    return any(k in lower for k in SEARCH_KEYWORDS)


def get_conversation_history(elder_id: int, session_id: Optional[str] = None, max_turns: int = 6) -> List[Dict[str, str]]:
    """Lấy danh sách các lượt hội thoại gần nhất của người cao tuổi theo phiên."""
    if not SESSIONS_FILE.exists():
        return []
    try:
        data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        elder_key = str(elder_id)
        elder_data = data.get(elder_key, {})

        # Nếu có session_id cụ thể, ưu tiên lấy lịch sử riêng biệt của phiên đó
        if session_id:
            sessions_map = elder_data.get("sessions", {})
            if session_id in sessions_map:
                return sessions_map[session_id][-max_turns:]
            # Nếu session_id trùng với phiên đang lưu trên elder_data
            if elder_data.get("session_id") == session_id:
                return elder_data.get("history", [])[-max_turns:]
            # Phiên mới hoàn toàn chưa có lịch sử
            return []

        history = elder_data.get("history", [])
        return history[-max_turns:]
    except Exception as e:
        logger.debug("Lỗi đọc session history: %s", e)
        return []


def save_conversation_turn(elder_id: int, user_text: str, ai_reply: str, session_id: Optional[str] = None) -> None:
    """Lưu một lượt trò chuyện vào session history an toàn nguyên tử, bảo đảm cách ly phiên."""
    if not user_text or not ai_reply:
        return
    try:
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if SESSIONS_FILE.exists():
            try:
                data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        elder_key = str(elder_id)
        if elder_key not in data:
            data[elder_key] = {
                "session_id": session_id,
                "history": [],
                "sessions": {},
                "updated_at": utc_now_iso()
            }

        turn_obj = {
            "user": user_text.strip(),
            "ai": ai_reply.strip(),
            "timestamp": utc_now_iso()
        }

        # Lưu vào lịch sử chung của elder (tối đa 20 lượt)
        elder_history = data[elder_key].get("history", [])
        elder_history.append(turn_obj)
        data[elder_key]["history"] = elder_history[-20:]
        data[elder_key]["session_id"] = session_id or data[elder_key].get("session_id")
        data[elder_key]["updated_at"] = utc_now_iso()

        # Nếu có session_id, lưu đồng thời vào khoang phiên riêng biệt
        if session_id:
            sessions_map = data[elder_key].setdefault("sessions", {})
            sess_list = sessions_map.setdefault(session_id, [])
            sess_list.append(turn_obj)
            sessions_map[session_id] = sess_list[-20:]

        tmp_path = SESSIONS_FILE.with_name(f"sessions.{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, SESSIONS_FILE)
    except Exception as e:
        logger.warning("Không thể lưu session turn: %s", e)


def clear_conversation_history(elder_id: int, session_id: Optional[str] = None) -> None:
    """Xóa lịch sử hội thoại của người cao tuổi (theo phiên hoặc toàn bộ)."""
    if not SESSIONS_FILE.exists():
        return
    try:
        data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        elder_key = str(elder_id)
        if elder_key in data:
            if session_id:
                sessions_map = data[elder_key].get("sessions", {})
                if session_id in sessions_map:
                    del sessions_map[session_id]
                if data[elder_key].get("session_id") == session_id:
                    data[elder_key]["history"] = []
            else:
                del data[elder_key]

            tmp_path = SESSIONS_FILE.with_name(f"sessions.{os.getpid()}.tmp")
            tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_path, SESSIONS_FILE)
    except Exception as e:
        logger.warning("Không thể xóa session history: %s", e)

# Cache runtime keys
_runtime_gemini_key: Optional[str] = None
_runtime_openai_key: Optional[str] = None
_runtime_openai_base: Optional[str] = None

# Cache thời tiết theo thời gian thực (15 phút một lần để tối ưu mạng)
_weather_cache: Dict[str, Any] = {"timestamp": 0, "summary": None}


def _mask_api_key_in_string(text: str) -> str:
    """Xóa bỏ/mask API key khỏi chuỗi để không bao giờ ghi key vào log."""
    if not text:
        return ""
    masked = re.sub(r'AIza[a-zA-Z0-9_\-]{30,}', 'AIza***', str(text))
    masked = re.sub(r'(?:key=|api_key=)[a-zA-Z0-9_\-]+', 'key=***', masked)
    return masked


# ==============================================================================
# CẤU HÌNH TRỢ LÝ GIỌNG ĐỌC DUY NHẤT: CHỊ GOOGLE (VI-VN-GOOGLE)
# Giọng đọc chuẩn mực, quen thuộc, rõ ràng, không gây bối rối cho Người cao tuổi
# ==============================================================================
VOICE_AGENT_PERSONAS: Dict[str, Dict[str, Any]] = {
    "vi-VN-Google": {
        "id": "google",
        "name": "Chị Google",
        "title": "Trợ lý Y tế Thông thái • Quen thuộc & Chuẩn xác",
        "gender": "female",
        "greeting_name": "cháu",
        "role_desc": "Người cháu trợ lý y tế với giọng đọc Chị Google quen thuộc, ân cần, rõ ràng, luôn túc trực bầu bạn và chăm lo sức khỏe cho Bác.",
        "thinking_style": (
            "Rất khoa học, chính xác từng khung giờ, từng cữ thuốc, từng chỉ số thời tiết thực tế. "
            "Có tư duy và thấu cảm sâu sắc: khi Bác hỏi thời tiết thì trả lời thời tiết thật và dặn dò chu đáo; "
            "khi Bác buồn cô đơn thì an ủi, bầu bạn ấm áp; khi Bác hỏi thuốc thì hướng dẫn cặn kẽ chuẩn y khoa."
        ),
        "tone": "Rõ ràng, gần gũi, ân cần, quen thuộc, chuẩn xác y tế",
        "speech_prefix": "Dạ thưa Bác, cháu luôn bên Bác đây ạ..."
    }
}


def is_valid_api_key(key: Optional[str]) -> bool:
    """Kiểm tra xem khóa API có hợp lệ không, loại bỏ các giá trị placeholder/mock/test."""
    if not key:
        return False
    k = key.strip()
    if len(k) < 15:
        return False
    dummy_patterns = ["mock", "testingkey", "example", "change-me", "fake", "placeholder", "secretkey"]
    if any(p in k.lower() for p in dummy_patterns):
        return False
    return True


def get_agent_info(voice: str = None) -> Dict[str, Any]:
    """Lấy thông tin Trợ lý giọng đọc duy nhất Chị Google."""
    return VOICE_AGENT_PERSONAS["vi-VN-Google"]


def get_gemini_key() -> str:
    global _runtime_gemini_key
    if _runtime_gemini_key:
        return _runtime_gemini_key
    return os.getenv("GEMINI_API_KEY", "").strip()


def get_openai_key() -> str:
    global _runtime_openai_key
    if _runtime_openai_key:
        return _runtime_openai_key
    return os.getenv("OPENAI_API_KEY", "").strip()


def get_openai_base() -> Optional[str]:
    global _runtime_openai_base
    if _runtime_openai_base:
        return _runtime_openai_base
    base = os.getenv("OPENAI_API_BASE") or os.getenv("OPENAI_BASE_URL")
    return base.strip() if base else None


def set_llm_config(gemini_key: str = None, openai_key: str = None, openai_base: str = None) -> bool:
    """Lưu API key vào runtime và cập nhật file .env an toàn với định dạng chuẩn."""
    global _runtime_gemini_key, _runtime_openai_key, _runtime_openai_base

    if gemini_key is not None:
        _runtime_gemini_key = gemini_key.strip()
        os.environ["GEMINI_API_KEY"] = _runtime_gemini_key
    if openai_key is not None:
        _runtime_openai_key = openai_key.strip()
        os.environ["OPENAI_API_KEY"] = _runtime_openai_key
    if openai_base is not None:
        _runtime_openai_base = openai_base.strip()
        os.environ["OPENAI_API_BASE"] = _runtime_openai_base

    for env_path in [ROOT_DIR / ".env", BASE_DIR / ".env"]:
        try:
            if not env_path.exists():
                continue

            with open(env_path, "r", encoding="utf-8") as f:
                content = f.read()

            lines = content.splitlines()
            new_lines = []
            keys_updated = {"GEMINI_API_KEY": False, "OPENAI_API_KEY": False}

            for line in lines:
                if gemini_key is not None and line.startswith("GEMINI_API_KEY="):
                    new_lines.append(f"GEMINI_API_KEY={_runtime_gemini_key}")
                    keys_updated["GEMINI_API_KEY"] = True
                elif openai_key is not None and line.startswith("OPENAI_API_KEY="):
                    new_lines.append(f"OPENAI_API_KEY={_runtime_openai_key}")
                    keys_updated["OPENAI_API_KEY"] = True
                else:
                    new_lines.append(line)

            if gemini_key is not None and not keys_updated["GEMINI_API_KEY"] and _runtime_gemini_key:
                new_lines.append(f"GEMINI_API_KEY={_runtime_gemini_key}")
            if openai_key is not None and not keys_updated["OPENAI_API_KEY"] and _runtime_openai_key:
                new_lines.append(f"OPENAI_API_KEY={_runtime_openai_key}")

            with open(env_path, "w", encoding="utf-8") as f:
                f.write("\n".join(new_lines) + "\n")
            logger.info("Đã lưu cấu hình LLM vào %s thành công.", env_path.name)
            return True
        except Exception as e:
            logger.warning("Không thể ghi cấu hình LLM vào %s: %s", env_path.name, _mask_api_key_in_string(str(e)))

    return True


def get_realtime_weather_summary(lat: float = 21.0285, lon: float = 105.8542) -> str:
    """
    Lấy thông tin thời tiết thực tế tại Việt Nam qua Open-Meteo API (100% miễn phí, không cần key).
    Dùng để cung cấp dữ liệu sống cho Trợ lý AI khi Bác hỏi 'Hôm nay thời tiết thế nào'.
    """
    global _weather_cache
    now_ts = time.time()
    if _weather_cache["summary"] and (now_ts - _weather_cache["timestamp"] < 900):
        return _weather_cache["summary"]

    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,weather_code"
        req = urllib.request.Request(url, headers={"User-Agent": "MediGuard-AI/2.0"})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            curr = data.get("current", {})
            temp = round(curr.get("temperature_2m", 28))
            humidity = curr.get("relative_humidity_2m", 60)
            code = curr.get("weather_code", 0)

            if code == 0:
                condition = "trời trong xanh, nắng ấm chan hòa"
            elif code in [1, 2]:
                condition = "trời nắng ráo, có mây nhẹ thoáng mát"
            elif code == 3:
                condition = "trời nhiều mây, râm mát"
            elif code in [45, 48]:
                condition = "trời có sương mù nhẹ, se se lạnh"
            elif code in [51, 53, 55, 61, 63, 65]:
                condition = "trời có mưa rào nhẹ hạt"
            elif code in [80, 81, 82, 95]:
                condition = "trời có mưa to kèm dông"
            else:
                condition = "thời tiết khá dễ chịu"

            summary = f"{condition}, nhiệt độ khoảng {temp} độ C, độ ẩm {humidity}%"
            _weather_cache = {"timestamp": now_ts, "summary": summary}
            return summary
    except Exception as e:
        logger.debug("Không thể lấy thời tiết trực tuyến: %s", e)
        return "thời tiết dịu mát, nhiệt độ ngoài trời khoảng 28 độ C"


def build_elderly_care_system_prompt(
    elder_name: str,
    medications: List[str] = None,
    time_context: str = None,
    weather_context: str = None,
    voice: str = "vi-VN-Google",
    history: Optional[List[Dict[str, str]]] = None
) -> str:
    """Tạo System Prompt tối ưu cho Agent cụ thể với tư duy và phong thái riêng biệt."""
    agent = get_agent_info(voice)
    meds_str = ", ".join(medications) if medications else "Hiện chưa có đơn thuốc đặc biệt ghi nhận"
    time_info = time_context or "Hiện tại"
    weather_info = weather_context or get_realtime_weather_summary()

    history_section = ""
    if history:
        lines = []
        for h in history:
            u = h.get("user")
            a = h.get("ai")
            if u:
                lines.append(f"- Bác từng nói: {u}")
            if a:
                lines.append(f"- Cháu từng trả lời: {a}")
        if lines:
            history_section = (
                "\nLỊCH SỬ CÁC LƯỢT HỘI THOẠI GẦN ĐÂY VỚI BÁC (DÙNG ĐỂ DUY TRÌ NGỮ CẢNH VÀ TRẢ LỜI CÂU HỎI LIÊN TỤC):\n"
                + "\n".join(lines) + "\n"
            )

    return f"""Bạn là Trợ Lý Đồng Hành AI kiêm Chăm Sóc Sức Khỏe có tên: {agent['name']}.
Bạn hoạt động như một người bạn tâm giao, một chatbot hội thoại thông thái, ấm áp, tương tự ChatGPT nhưng được thiết kế chuyên biệt, kính cẩn và thấu hiểu người cao tuổi Việt Nam.
Danh tính & Vai trò: {agent['role_desc']}
Phong cách suy nghĩ: {agent['thinking_style']}
Tông giọng: {agent['tone']}
Cách xưng hô và đại từ nhân xưng:
- BẮT BUỘC luôn xưng 'cháu' và gọi người dùng là 'Bác' (hoặc 'Cụ') trong mọi câu trả lời.
- Mọi câu trả lời của bạn BẮT BUỘC phải xuất hiện rõ từ 'cháu' và từ 'Bác' (ví dụ: 'Dạ thưa Bác, cháu xin thưa...', 'cháu kính chúc Bác...').
- Tuyệt đối không xưng 'tôi', 'mình' hay nói chung chung không có đại từ 'cháu'.

NGỮ CẢNH HIỆN TẠI CỦA BÁC:
- Bác đang trò chuyện cùng bạn tên là: {elder_name}.
- Thời gian hiện tại: {time_info}.
- Thời tiết thực tế hôm nay ngoài trời: {weather_info}.
- Đơn thuốc của Bác trong hệ thống: {meds_str}.{history_section}

QUY TẮC SUY NGHĨ VÀ TRẢ LỜI CỦA BẠN:
1. KHẢ NĂNG TRÒ CHUYỆN TỔNG QUÁT:
   - Bạn sẵn sàng trả lời linh hoạt, sâu sắc mọi câu hỏi mở rộng: kiến thức khoa học, lịch sử, văn hóa dân gian, giải thích đời sống, kể chuyện vui, câu đố vui, tâm sự xua tan cô đơn, hoài niệm ký ức.
   - Khi Bác hỏi tiếp nối các câu chuyện cũ hoặc hỏi lại thông tin Bác từng chia sẻ (tên con cháu, quê quán, sở thích), hãy nhớ và liên kết chính xác từ LỊCH SỬ HỘI THOẠI ở trên.
2. ĐỘ DÀI & VĂN PHONG PHÙ HỢP GIỌNG NÓI:
   - Với lời chào hỏi, hỏi thăm sức khỏe thông thường: trả lời ngắn gọn trong 2-3 câu (khoảng 35-50 từ).
   - Với câu hỏi kiến thức, giải thích khoa học, lịch sử, kể chuyện cổ tích, câu đố: trả lời đầy đủ ý, sinh động, dễ hiểu, súc tích (khoảng 2-4 đoạn ngắn).
   - TUYỆT ĐỐI KHÔNG DÙNG KÝ HIỆU MARKDOWN như dấu sao (*), thăng (#), gạch đầu dòng (-), hay emoji, vì toàn bộ câu trả lời sẽ được chuyển thành âm thanh đọc to cho người già nghe. Chỉ dùng câu văn xuôi tiếng Việt tự nhiên, ấm áp, liền mạch.
3. AN TOÀN Y TẾ VÀ TƯ VẤN SỨC KHỎE:
   - Bạn chỉ tư vấn dinh dưỡng, lối sống lành mạnh, nhắc uống thuốc đúng giờ. Tuyệt đối không tự ý kê đơn thuốc mới, không bảo Bác tự ý thay đổi liều lượng thuốc.
   - Khi Bác hỏi về các bệnh mãn tính (huyết áp, tim mạch, tiểu đường): luôn ân cần dặn Bác phải uống thuốc đúng giờ theo đơn bác sĩ chỉ định, ăn uống giảm mặn (hạn chế muối), ăn nhiều rau xanh và tham khảo ý kiến bác sĩ khi cần.
4. TÌM KIẾM INTERNET VÀ TÍNH TRUNG THỰC:
   - Nếu bạn được cung cấp dữ liệu tìm kiếm Internet, hãy cập nhật thông tin chuẩn xác. Nếu không có kết quả tìm kiếm hoặc tìm kiếm thất bại, hãy nói rõ giới hạn, không tự bịa đặt nguồn tin hay sự kiện."""


async def query_gemini_llm(
    prompt: str,
    system_instruction: str,
    api_key: str,
    use_grounding: bool = False,
    return_metadata: bool = False,
    history: Optional[List[Dict[str, str]]] = None
) -> Any:
    """
    Gọi Google Gemini Flash API thông qua google.genai SDK (v2+).
    Hỗ trợ:
    - Model chính: settings.GEMINI_FAST_MODEL (mặc định gemini-3.5-flash-lite).
    - Ưu tiên Interactions API (client.interactions.create) và fallback sang models.generate_content.
    - Google Search grounding khi use_grounding=True.
    - Xử lý an toàn các lỗi: auth_error (401/403), rate_limit tạm thời (429), quota_exhausted, service_unavailable (503), timeout, empty_response.
    - Giữ trọn vẹn ngữ cảnh lịch sử hội thoại (history).
    - Không bịa đặt citations, URLs, search queries nếu không có trong metadata phản hồi thật.
    - Tuyệt đối không ghi API key vào log.
    """
    clean_key = (api_key or "").strip()
    primary_model = settings.GEMINI_FAST_MODEL or "gemini-3.5-flash-lite"

    if not is_valid_api_key(clean_key):
        if return_metadata:
            return {
                "text": None,
                "model": None,
                "grounding_used": False,
                "grounding_queries": [],
                "sources": [],
                "error_type": "missing_api_key"
            }
        return None

    # Bảo đảm history được truyền tải nếu system_instruction chưa chứa section lịch sử
    history_ctx = ""
    if history and "LỊCH SỬ CÁC LƯỢT HỘI THOẠI" not in system_instruction:
        h_lines = []
        for h in history:
            u = h.get("user")
            a = h.get("ai")
            if u:
                h_lines.append(f"- Bác từng nói: {u}")
            if a:
                h_lines.append(f"- Cháu từng trả lời: {a}")
        if h_lines:
            history_ctx = (
                "\nLỊCH SỬ CÁC LƯỢT HỘI THOẠI GẦN ĐÂY VỚI BÁC (DÙNG ĐỂ DUY TRÌ NGỮ CẢNH):\n"
                + "\n".join(h_lines) + "\n"
            )

    full_system = f"{system_instruction}{history_ctx}".strip()
    full_content = f"{full_system}\n\nCâu Bác hỏi: \"{prompt}\"\nCâu trả lời bằng giọng nói:"
    last_error_type = None

    # Thử gọi qua google.genai SDK (ưu tiên Interactions API cho gemini-3.5-flash-lite)
    max_retries = 2
    for attempt in range(1, max_retries + 1):
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=clean_key)
            tools = None
            if use_grounding:
                try:
                    tools = [{"google_search": {}}]
                except Exception as cfg_err:
                    logger.debug("Không thể khởi tạo Google Search Tool config: %s", cfg_err)
                    last_error_type = "search_error"
                    tools = None

            sdk_timeout = 8.5

            def _call_gemini_sdk():
                # 1. Ưu tiên Interactions API (đã kiểm thử thành công độc lập trên 2.30.1)
                if hasattr(client, "interactions") and hasattr(client.interactions, "create") and callable(client.interactions.create):
                    create_kwargs = {
                        "model": primary_model,
                        "input": full_content,
                        "timeout": sdk_timeout,
                    }
                    if tools:
                        create_kwargs["tools"] = tools
                    try:
                        inter_res = client.interactions.create(**create_kwargs)
                        # Xác minh kết quả trả về là một Interaction hợp lệ hoặc dict có text thực tế
                        out_val = getattr(inter_res, "output_text", None)
                        if isinstance(out_val, str) and out_val.strip():
                            return inter_res
                        if isinstance(inter_res, dict) and (inter_res.get("output_text") or inter_res.get("text")):
                            return inter_res
                    except (AttributeError, TypeError):
                        pass

                # 2. Fallback sang models.generate_content nếu interactions không khả dụng hoặc mock cấu hình cho generate_content
                gen_config = None
                if use_grounding:
                    try:
                        gen_config = types.GenerateContentConfig(
                            temperature=0.7,
                            max_output_tokens=300,
                            tools=[types.Tool(google_search=types.GoogleSearch())]
                        )
                    except Exception:
                        gen_config = None

                return client.models.generate_content(
                    model=primary_model,
                    contents=full_content,
                    config=gen_config
                )

            response = await asyncio.wait_for(
                asyncio.to_thread(_call_gemini_sdk),
                timeout=10.0
            )

            if response:
                reply_text = getattr(response, "output_text", None)
                if not isinstance(reply_text, str) or not reply_text.strip():
                    reply_text = getattr(response, "text", None)
                if isinstance(response, dict):
                    reply_text = response.get("output_text") or response.get("text") or ""
                if not isinstance(reply_text, str):
                    reply_text = ""

                cleaned = reply_text.strip().replace("*", "").replace("#", "")

                # Phản hồi rỗng: coi như lỗi, không coi là thành công
                if not cleaned or len(cleaned) < 3:
                    last_error_type = "empty_response"
                    logger.warning("Gemini trả về phản hồi rỗng hoặc bị chặn an toàn.")
                    if return_metadata:
                        return {
                            "text": None,
                            "model": primary_model,
                            "grounding_used": False,
                            "grounding_queries": [],
                            "sources": [],
                            "error_type": "empty_response"
                        }
                    return None

                grounding_chunks = []
                search_queries = []

                # A. Trích xuất metadata từ Interactions API steps
                steps = getattr(response, "steps", None)
                if isinstance(steps, (list, tuple)):
                    for step in steps:
                        step_type = getattr(step, "type", None)
                        if step_type == "google_search_call":
                            args = getattr(step, "arguments", None)
                            if args:
                                q_list = getattr(args, "queries", None)
                                if isinstance(q_list, (list, tuple)):
                                    search_queries.extend([str(q) for q in q_list if q])
                        elif step_type == "google_search_result":
                            res_list = getattr(step, "result", None)
                            if isinstance(res_list, (list, tuple)):
                                for r in res_list:
                                    url = getattr(r, "url", None) or getattr(r, "uri", None)
                                    title = getattr(r, "title", None)
                                    if url or title:
                                        grounding_chunks.append({"title": str(title) if title else "", "url": str(url) if url else ""})

                # B. Trích xuất metadata từ generate_content candidates
                cands = getattr(response, "candidates", None)
                if isinstance(cands, (list, tuple)) and len(cands) > 0:
                    g_meta = getattr(cands[0], "grounding_metadata", None)
                    if g_meta:
                        sq = getattr(g_meta, "web_search_queries", None)
                        if isinstance(sq, (list, tuple)):
                            search_queries.extend([str(q) for q in sq if q])
                        chunks = getattr(g_meta, "grounding_chunks", None)
                        if isinstance(chunks, (list, tuple)):
                            for ch in chunks:
                                web_info = getattr(ch, "web", None)
                                if web_info:
                                    uri = getattr(web_info, "uri", None)
                                    title = getattr(web_info, "title", None)
                                    if uri or title:
                                        grounding_chunks.append({"title": str(title) if title else "", "url": str(uri) if uri else ""})

                # Không tự bịa đặt metadata; chỉ đánh dấu True khi có bằng chứng thực tế
                grounding_used = bool(search_queries or grounding_chunks)

                if return_metadata:
                    return {
                        "text": cleaned,
                        "model": primary_model,
                        "grounding_used": grounding_used,
                        "grounding_queries": search_queries,
                        "sources": grounding_chunks,
                        "error_type": None
                    }
                return cleaned

        except asyncio.TimeoutError:
            last_error_type = "timeout"
            logger.warning("Gemini model %s quá thời gian chờ (Timeout 10s).", primary_model)
            break
        except Exception as model_err:
            err_str = _mask_api_key_in_string(str(model_err))

            # 401 / 403 / API_KEY_INVALID: lỗi xác thực / quyền -> Dừng ngay, không retry
            if any(c in err_str for c in ["400", "401", "403", "API_KEY_INVALID", "PERMISSION_DENIED"]):
                last_error_type = "auth_error"
                logger.warning("Khóa Gemini API không hợp lệ hoặc thiếu quyền (Auth Error): %s", err_str[:80])
                if return_metadata:
                    return {
                        "text": None,
                        "model": primary_model,
                        "grounding_used": False,
                        "grounding_queries": [],
                        "sources": [],
                        "error_type": "auth_error"
                    }
                return None

            # 429 Quota Exhausted -> Không retry, không gọi hàng loạt
            if "RESOURCE_EXHAUSTED" in err_str or "QUOTA_EXCEEDED" in err_str or "quota exceeded" in err_str.lower():
                last_error_type = "quota_exhausted"
                logger.warning("Gemini API hết quota (ResourceExhausted / Quota Exceeded).")
                if return_metadata:
                    return {
                        "text": None,
                        "model": primary_model,
                        "grounding_used": False,
                        "grounding_queries": [],
                        "sources": [],
                        "error_type": "quota_exhausted"
                    }
                return None

            # 429 Rate limit tạm thời -> Thử lại có backoff nếu còn lượt
            if "429" in err_str or "RATE_LIMIT" in err_str or "rate limit" in err_str.lower():
                last_error_type = "rate_limit"
                if attempt < max_retries:
                    logger.warning("Gemini API bị giới hạn tần suất tạm thời (Rate Limit 429). Thử lại sau 1s...")
                    await asyncio.sleep(1.0)
                    continue
                else:
                    logger.warning("Gemini API tiếp tục bị giới hạn tần suất (429) sau khi thử lại.")
                    break

            # 503 / UNAVAILABLE / Quá tải -> Thử lại 1 lần nếu còn lượt
            if "503" in err_str or "UNAVAILABLE" in err_str or "high demand" in err_str.lower() or "overloaded" in err_str.lower():
                last_error_type = "service_unavailable"
                if attempt < max_retries:
                    logger.warning("Gemini API bị quá tải hoặc tạm thời không khả dụng (503). Thử lại sau 1.5s...")
                    await asyncio.sleep(1.5)
                    continue
                else:
                    logger.warning("Gemini API vẫn bị quá tải (503) sau khi thử lại.")
                    break

            logger.debug("Gemini model %s lỗi: %s", primary_model, err_str)
            last_error_type = "llm_unavailable"
            break

    # Ghi chú kiến trúc: Đã loại bỏ vòng lặp fallback REST cũ (gemini-1.5-flash, gemini-2.0-flash)
    # vì các model đó đã lỗi thời/bị vô hiệu hóa trên API, gây thêm 5-10s chờ vô ích khi SDK lỗi.
    if return_metadata:
        return {
            "text": None,
            "model": primary_model,
            "grounding_used": False,
            "grounding_queries": [],
            "sources": [],
            "error_type": last_error_type or "llm_unavailable"
        }
    return None


async def query_openai_llm(prompt: str, system_instruction: str, api_key: str, base_url: str = None) -> Optional[str]:
    """Gọi OpenAI / Groq / OpenRouter / Ollama."""
    try:
        from openai import OpenAI

        if api_key.startswith("gsk_") and not base_url:
            base_url = "https://api.groq.com/openai/v1"
            model = "llama-3.3-70b-versatile"
        elif not base_url or "openai" in base_url:
            model = "gpt-4o-mini"
        else:
            model = "llama-3.3-70b-versatile"

        client = OpenAI(api_key=api_key, base_url=base_url)

        response = await asyncio.wait_for(
            asyncio.to_thread(
                client.chat.completions.create,
                model=model,
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=200,
                temperature=0.7
            ),
            timeout=4.5
        )
        if response and response.choices and response.choices[0].message.content:
            return response.choices[0].message.content.strip().replace("*", "").replace("#", "")
    except Exception as e:
        logger.warning("OpenAI/Groq LLM gọi thất bại: %s", _mask_api_key_in_string(str(e)))
    return None


def detect_drug_lookup(text: str) -> Optional[dict]:
    """
    Nhận diện câu hỏi tra cứu thông tin dược lý / công dụng / tác dụng phụ của thuốc.
    Phân biệt rõ với câu hỏi lịch uống thuốc cá nhân hoặc đơn thuốc của người bệnh.
    """
    if not text:
        return None
    text_clean = text.lower().strip()
    text_norm = re.sub(r'[?!.,;:]+$', '', text_clean).strip()

    # 1. Bỏ qua nếu là câu hỏi về đơn thuốc cá nhân hoặc lịch uống thuốc của người bệnh
    personal_prescription_triggers = [
        "trong đơn", "đơn thuốc của", "thuốc của tôi", "thuốc của bác", "thuốc của cụ",
        "tôi đang uống", "bác đang uống", "cụ đang uống",
        "tôi có đang uống", "bác có đang uống", "tôi có phải uống", "bác có phải uống",
        "mấy giờ uống", "khi nào uống", "lịch uống", "cữ sáng", "cữ trưa", "cữ chiều", "cữ tối",
        "hôm nay uống", "hôm nay tôi uống", "hôm nay bác uống", "đã uống", "uống rồi",
        "kế hoạch hôm nay", "việc cần làm hôm nay", "nhiệm vụ hôm nay"
    ]
    if any(p in text_norm for p in personal_prescription_triggers):
        return None

    # 2. Bỏ qua nếu là câu hỏi tư vấn cách uống / thời điểm / tương tác đồ ăn thức uống chung
    ingestion_lifestyle_triggers = [
        "thời điểm nào tốt nhất", "uống vào lúc nào tốt nhất", "uống lúc nào tốt nhất",
        "uống cùng lúc với sữa", "uống với sữa", "uống với nước gì", "uống nước gì",
        "uống bao nhiêu nước", "uống trước hay sau ăn", "lúc đói hay lúc no"
    ]
    if any(t in text_norm for t in ingestion_lifestyle_triggers):
        return None

    # 3. Nhận diện khía cạnh câu hỏi (aspect)
    aspect = None
    if re.search(r'tác dụng phụ|tác dụng không mong muốn|phản ứng phụ|phản ứng không mong muốn', text_norm):
        aspect = "side_effects"
    elif re.search(r'chống chỉ định|ai không nên (?:uống|dùng)|ai không được (?:uống|dùng)|không dùng cho ai', text_norm):
        aspect = "contraindications"
    elif re.search(r'tương tác|kỵ với|kiêng gì|kỵ gì', text_norm):
        aspect = "interactions"
    elif re.search(r'hoạt chất|thành phần', text_norm):
        aspect = "ingredient"
    elif re.search(r'dùng để làm gì|dùng làm gì|dùng để trị gì|dùng trị gì|dùng để chữa gì|dùng chữa gì|trị bệnh gì|chữa bệnh gì|trị cái gì|chữa cái gì|có tác dụng gì|tác dụng gì|công dụng gì|có công dụng gì|công dụng của|tác dụng của', text_norm):
        aspect = "indication"
    elif re.search(r'là thuốc gì|thông tin về thuốc|thông tin thuốc|tra cứu thuốc|tìm hiểu về thuốc|cho tôi biết về thuốc|cho tôi biết thuốc|cho bác biết về thuốc|cho bác biết thuốc', text_norm):
        aspect = "general"

    if not aspect:
        return None

    # 4. Bóc tách tên thuốc mục tiêu (drug name)
    cleaned = text_norm
    prefixes = [
        r'^(?:dạ\s+)?(?:thưa\s+cháu\s+)?(?:cháu\s+ơi\s+)?',
        r'^cho\s+(?:tôi|bác|cháu|em|mình)\s+(?:hỏi\s+)?(?:biết\s+)?(?:thông\s+tin\s+)?(?:về\s+)?',
        r'^(?:hãy\s+)?cho\s+(?:tôi|bác|cháu|em)\s+hỏi\s+(?:về\s+)?',
        r'^làm\s+ơn\s+cho\s+(?:tôi|bác)\s+biết\s+(?:về\s+)?',
        r'^tra\s+cứu\s+(?:thông\s+tin\s+)?(?:về\s+)?',
        r'^tìm\s+hiểu\s+(?:về\s+)?',
        r'^thông\s+tin\s+(?:về\s+)?',
        r'^bác\s+muốn\s+(?:hỏi|biết)\s+(?:về\s+)?',
        r'^tôi\s+muốn\s+(?:hỏi|biết)\s+(?:về\s+)?',
    ]
    for p in prefixes:
        cleaned = re.sub(p, '', cleaned).strip()

    # Mẫu A: (công dụng|tác dụng|tác dụng phụ|thành phần|hoạt chất) của (thuốc )?X
    m_of = re.search(r'(?:công dụng|tác dụng|tác dụng phụ|thành phần|hoạt chất)\s+của\s+(?:thuốc\s+|viên\s+)?(.+)', cleaned)
    if m_of:
        raw_drug = m_of.group(1).strip()
        raw_drug = re.sub(r'\s+(?:là gì|như thế nào|thế nào|hả cháu|nhỉ|vậy).*$', '', raw_drug).strip()
        return {"drug_name": raw_drug, "aspect": aspect}

    # Mẫu B: (thuốc |viên )?X + hậu tố câu hỏi
    suffixes = [
        r'\s+(?:dùng\s+để\s+làm\s+gì|dùng\s+làm\s+gì|dùng\s+để\s+trị\s+gì|dùng\s+trị\s+gì|trị\s+bệnh\s+gì|chữa\s+bệnh\s+gì).*$',
        r'\s+(?:có\s+tác\s+dụng\s+gì|tác\s+dụng\s+gì|công\s+dụng\s+gì|có\s+công\s+dụng\s+gì).*$',
        r'\s+(?:có\s+tác\s+dụng\s+phụ\s+gì|tác\s+dụng\s+phụ\s+gì|tác\s+dụng\s+phụ\s+là\s+gì).*$',
        r'\s+(?:chống\s+chỉ\s+định\s+gì|chống\s+chỉ\s+định\s+như\s+thế\s+nào).*$',
        r'\s+(?:là\s+thuốc\s+gì|là\s+gì).*$',
        r'\s+(?:kỵ\s+với\s+gì|kiêng\s+gì).*$',
    ]
    for s in suffixes:
        if re.search(s, cleaned):
            raw_drug = re.sub(s, '', cleaned).strip()
            raw_drug = re.sub(r'^(?:thuốc|viên|loại\s+thuốc)\s+', '', raw_drug).strip()
            if raw_drug:
                return {"drug_name": raw_drug, "aspect": aspect}

    # Mẫu C: (về )?(thuốc |viên )X
    m_thuoc = re.match(r'^(?:về\s+)?(?:thuốc|viên)\s+([a-zA-Z0-9\sà-ỹÀ-Ỹ\-\_\.]+)', cleaned)
    if m_thuoc:
        raw_drug = m_thuoc.group(1).strip()
        raw_drug = re.sub(r'\s+(?:là gì|như thế nào|thế nào|hả cháu|nhỉ|vậy).*$', '', raw_drug).strip()
        return {"drug_name": raw_drug, "aspect": aspect}

    # Mẫu D: Nếu câu hỏi có dạng "cho tôi biết thuốc X"
    if cleaned and aspect in ["general", "indication"]:
        clean_name = re.sub(r'^(?:thuốc|viên|về\s+thuốc|về\s+viên)\s+', '', cleaned).strip()
        clean_name = re.sub(r'\s+(?:là gì|như thế nào|thế nào|hả cháu|nhỉ|vậy).*$', '', clean_name).strip()
        if clean_name and len(clean_name) >= 2:
            return {"drug_name": clean_name, "aspect": aspect}

    return None


def _format_verified_drug_reply(
    drug_dict: Dict[str, Any],
    aspect: str = "general",
    elder_name: str = "Bác",
    is_elder: bool = True
) -> str:
    """Định dạng phản hồi thông tin dược lý chuẩn hóa từ dữ liệu thuốc đã xác minh."""
    drug_name = drug_dict.get("name", "Thuốc")
    category = drug_dict.get("category", "Thuốc điều trị")
    indication = drug_dict.get("indication", "Chưa có thông tin chỉ định chi tiết.")
    active_ing = drug_dict.get("active_ingredient", "")
    ing_part = f" (hoạt chất {active_ing})" if active_ing else ""
    side_effects = drug_dict.get("side_effects", "Chưa có ghi nhận tác dụng phụ điển hình.")
    contraindications = drug_dict.get("contraindications", "Chưa có thông tin chống chỉ định cụ thể.")
    interactions = drug_dict.get("interactions", "Chưa có thông tin tương tác thuốc cụ thể.")

    if aspect == "side_effects":
        if is_elder:
            return (
                f"Dạ thưa {elder_name}, theo thông tin dược lý chuẩn hóa, tác dụng phụ có thể gặp của thuốc {drug_name} gồm có: "
                f"{side_effects} "
                "Nếu Bác gặp các dấu hiệu bất thường này, Bác hãy báo ngay cho người nhà hoặc bác sĩ để được kiểm tra kịp thời nhé ạ!"
            )
        else:
            return (
                f"Dạ chào {elder_name}, các tác dụng phụ có thể gặp của thuốc {drug_name} gồm: "
                f"{side_effects} "
                "Nếu người bệnh xuất hiện các triệu chứng bất thường, hãy tham khảo ý kiến bác sĩ điều trị ạ."
            )
    elif aspect == "contraindications":
        if is_elder:
            return (
                f"Dạ thưa {elder_name}, thuốc {drug_name} có các chống chỉ định sau: {contraindications} "
                "Bác tuyệt đối không tự ý dùng nếu thuộc các trường hợp này nhé ạ!"
            )
        else:
            return (
                f"Dạ chào {elder_name}, các chống chỉ định của thuốc {drug_name} gồm: {contraindications} "
                "Tuyệt đối không sử dụng cho các đối tượng này ạ."
            )
    elif aspect == "interactions":
        if is_elder:
            return (
                f"Dạ thưa {elder_name}, về tương tác và kiêng kỵ của thuốc {drug_name}: {interactions} "
                "Bác chú ý lưu ý để uống thuốc an toàn nhé ạ!"
            )
        else:
            return (
                f"Dạ chào {elder_name}, tương tác thuốc của {drug_name}: {interactions}."
            )
    elif aspect == "ingredient":
        if is_elder:
            return f"Dạ thưa {elder_name}, thuốc {drug_name} có hoạt chất chính là: {active_ing}."
        else:
            return f"Dạ chào {elder_name}, hoạt chất chính của thuốc {drug_name} là: {active_ing}."
    else:  # general hoặc indication
        if is_elder:
            return (
                f"Dạ thưa {elder_name}, thuốc {drug_name}{ing_part} thuộc nhóm {category}. "
                f"Công dụng chính: {indication} "
                "Lưu ý: Thông tin mang tính tham khảo y tế chuẩn hóa, Bác chỉ dùng thuốc theo đúng chỉ định và hướng dẫn của bác sĩ điều trị nhé ạ!"
            )
        else:
            return (
                f"Dạ chào {elder_name}, thuốc {drug_name}{ing_part} thuộc nhóm {category}. "
                f"Công dụng chính: {indication} "
                "Lưu ý: Thông tin mang tính tham khảo y tế, cần tuân thủ chỉ định của bác sĩ chuyên khoa ạ."
            )


async def get_smart_llm_reply(
    user_text: str,
    elder_name: str = "Bác",
    medications: List[str] = None,
    time_context: str = None,
    weather_context: str = None,
    voice: str = "vi-VN-Google",
    use_grounding: Optional[bool] = None,
    history: Optional[List[Dict[str, str]]] = None,
    return_metadata: bool = False,
    intent: Optional[str] = None,
    drug_info: Optional[Any] = None,
    **kwargs
) -> Any:
    """
    Điểm vào trung tâm (Entry point) để Agent sinh phản hồi:
    1. Tự động nhận biết câu hỏi cần tra cứu Internet (Google Search grounding).
    2. Nạp ngữ cảnh hội thoại đa lượt (multi-turn history) để liên kết câu trả lời trước sau.
    3. Gọi Gemini / Groq / OpenAI sinh phản hồi mang phong thái riêng của Chị Google.
    4. Tự động fallback sang bộ não suy luận ngữ cảnh khi không có key hoặc mạng chập chờn.
    """
    clean_prompt = user_text.strip()
    if not clean_prompt:
        if return_metadata:
            return {"text": None, "model": None, "grounding_used": False, "sources": [], "error_type": "empty_prompt"}
        return None

    # Tự động nhận diện có cần tra cứu Internet nếu không được chỉ định
    if use_grounding is None:
        use_grounding = detect_search_need(clean_prompt)

    sys_prompt = build_elderly_care_system_prompt(
        elder_name=elder_name,
        medications=medications,
        time_context=time_context,
        weather_context=weather_context,
        voice=voice,
        history=history
    )

    gemini_key = get_gemini_key()
    has_valid_gemini = is_valid_api_key(gemini_key)
    last_error_type = "missing_api_key" if not has_valid_gemini else None

    # 1. Thử Google Gemini nếu có key hợp lệ
    if has_valid_gemini:
        try:
            llm_res = await query_gemini_llm(
                clean_prompt,
                sys_prompt,
                gemini_key,
                use_grounding=use_grounding,
                return_metadata=True,
                history=history
            )
            if llm_res and llm_res.get("text") and len(llm_res["text"]) >= 8:
                if return_metadata:
                    return llm_res
                return llm_res["text"]
            elif llm_res and llm_res.get("error_type"):
                last_error_type = llm_res["error_type"]
                # Nếu do search grounding bị lỗi công cụ, thử lại 1 lần không dùng grounding
                if use_grounding and last_error_type == "search_error":
                    retry_res = await query_gemini_llm(
                        clean_prompt,
                        sys_prompt,
                        gemini_key,
                        use_grounding=False,
                        return_metadata=True,
                        history=history
                    )
                    if retry_res and retry_res.get("text") and len(retry_res["text"]) >= 8:
                        retry_res["grounding_used"] = False
                        retry_res["search_failed"] = True
                        if return_metadata:
                            return retry_res
                        return retry_res["text"]
        except Exception as gemini_err:
            logger.warning("Gemini API gặp ngoại lệ kết nối/hệ thống: %s", _mask_api_key_in_string(str(gemini_err)))
            last_error_type = "service_unavailable"

    # 2. Thử OpenAI / Groq nếu có key hợp lệ
    openai_key = get_openai_key()
    if is_valid_api_key(openai_key):
        try:
            reply = await query_openai_llm(clean_prompt, sys_prompt, openai_key, get_openai_base())
            if reply and len(reply) >= 10:
                if return_metadata:
                    return {
                        "text": reply,
                        "model": "openai-compatible",
                        "grounding_used": False,
                        "grounding_queries": [],
                        "sources": [],
                        "error_type": None
                    }
                return reply
        except Exception as openai_err:
            logger.warning("OpenAI API gặp ngoại lệ: %s", _mask_api_key_in_string(str(openai_err)))
            last_error_type = "service_unavailable"

    # 3. Fallback sang bộ não suy luận ngữ cảnh lão khoa
    fallback_text = get_smart_contextual_fallback(
        user_text=clean_prompt,
        elder_name=elder_name,
        medications=medications,
        time_context=time_context,
        weather_context=weather_context,
        voice=voice,
        history=history,
        search_needed=use_grounding,
        error_type=last_error_type,
        intent=intent,
        drug_info=drug_info,
        **kwargs
    )

    if return_metadata:
        return {
            "text": fallback_text,
            "model": "contextual_fallback",
            "grounding_used": False,
            "grounding_queries": [],
            "sources": [],
            "error_type": last_error_type
        }
    return fallback_text


def get_smart_contextual_fallback(
    user_text: str,
    elder_name: str = "Bác",
    medications: List[str] = None,
    time_context: str = None,
    weather_context: str = None,
    voice: str = "vi-VN-Google",
    history: Optional[List[Dict[str, str]]] = None,
    search_needed: bool = False,
    error_type: Optional[str] = None,
    intent: Optional[str] = None,
    drug_info: Optional[Any] = None,
    **kwargs
) -> str:
    """
    Bộ não suy luận ngữ cảnh lão khoa thông minh độc quyền của MediGuard AI:
    Có tư duy sâu sắc, tương tự một người bạn tâm giao ấm áp khi offline hoặc API LLM chưa sẵn sàng:
    1. Truy hồi ngữ cảnh nhiều lượt (nhớ tên con cháu, sự kiện vừa trao đổi).
    2. Giải thích khoa học, kiến thức phổ thông rõ ràng, không dùng markdown rối mắt.
    3. Lịch sử, văn hóa dân gian Việt Nam hào hùng, gần gũi.
    4. Kể chuyện cổ tích, chuyện vui, câu đố dân gian giàu tình cảm.
    5. Minh bạch giới hạn tìm kiếm Internet: Không bịa đặt nguồn tin khi offline.
    6. Tra cứu thuốc an toàn chuẩn y khoa khi LLM offline: Không bịa đặt liều lượng hay chẩn đoán.
    7. Tư vấn dinh dưỡng, sinh hoạt, an toàn y tế và đồng hành xua tan cô đơn.
    """
    text_clean = user_text.lower().strip()
    weather_summary = weather_context or get_realtime_weather_summary()
    is_elder = not any(w in elder_name.lower() for w in ["bạn", "anh", "chị", "caregiver", "admin"])

    # =========================================================================
    # KIỂM TRA VÀ XỬ LÝ TRA CỨU THUỐC (DRUG LOOKUP ROUTING & FALLBACK)
    # =========================================================================
    lookup_meta = detect_drug_lookup(user_text) if user_text else None
    is_drug_query = (
        intent == "DRUG_LOOKUP" or
        bool(kwargs.get("is_drug_lookup")) or
        (drug_info is not None) or
        (lookup_meta is not None)
    )

    if is_drug_query:
        aspect = (lookup_meta.get("aspect") if lookup_meta else None) or kwargs.get("aspect") or "general"
        target_name = (lookup_meta.get("drug_name") if lookup_meta else None) or kwargs.get("drug_name") or ""

        # 1. Nếu luồng gọi đã truyền sẵn kết quả/dữ liệu thuốc (drug_info hợp lệ):
        # TUYỆT ĐỐI KHÔNG ĐƯỢC GHI ĐÈ KẾT QUẢ TRA CỨU BẰNG LỜI KHUYÊN CHUNG CHUNG
        if drug_info is not None:
            if isinstance(drug_info, str) and drug_info.strip():
                return drug_info.strip()
            if isinstance(drug_info, dict) and drug_info.get("name"):
                return _format_verified_drug_reply(drug_info, aspect=aspect, elder_name=elder_name, is_elder=is_elder)

        # 2. Phân biệt nhóm sản phẩm thảo dược chung "thuốc bổ phế"
        clean_text_for_drug = user_text.lower().strip()
        if "bổ phế" in target_name.lower() or "bổ phế" in clean_text_for_drug:
            if is_elder:
                return (
                    f"Dạ thưa {elder_name}, 'thuốc bổ phế' là tên gọi chung cho nhóm sản phẩm đông dược hoặc siro thảo dược "
                    "có tác dụng nhuận phế, bổ phổi, làm ấm đường hô hấp và giảm các triệu chứng ho, đau rát họng (như siro Bổ phế Nam Hà, Bổ phế Bảo Thanh...). "
                    "Tuy nhiên, đây là nhóm sản phẩm chung chứ chưa phải tên một loại thuốc cụ thể. "
                    "Để cháu hỗ trợ Bác chính xác nhất, Bác có thể cho cháu biết thêm tên đầy đủ của sản phẩm, nhà sản xuất, hoặc nhờ người nhà chụp ảnh bao bì/vỏ hộp gửi lên ứng dụng giúp Bác nhé ạ!"
                )
            else:
                return (
                    f"Dạ chào {elder_name}, 'thuốc bổ phế' là tên gọi chung cho nhóm sản phẩm siro hoặc đông dược thảo dược "
                    "có tác dụng nhuận phế, bổ phổi, làm ấm đường hô hấp và hỗ trợ giảm ho, long đờm (như Bổ phế Nam Hà, Bổ phế Bảo Thanh...). "
                    "Vì đây là tên nhóm sản phẩm chứ chưa phải một loại thuốc cụ thể, bạn vui lòng cung cấp tên đầy đủ của sản phẩm, nhà sản xuất, "
                    "hoặc chụp ảnh bao bì để hệ thống tra cứu thông tin chi tiết và chính xác nhé ạ."
                )

        # 3. Tra cứu trong cơ sở dữ liệu thuốc chuẩn hóa (DRUG_DATABASE)
        matched_drug = None
        search_term = target_name or user_text
        try:
            from app.api.drugs import search_drugs, DRUG_DATABASE
            search_res = search_drugs(search_term)
            candidates = search_res.get("results", []) if search_res else []
            if candidates:
                first = candidates[0]
                if any(d["id"] == first.get("id") for d in DRUG_DATABASE):
                    matched_drug = first
        except Exception as e:
            logger.debug("Tra cứu DB thuốc trong fallback: %s", e)
            matched_drug = None

        if matched_drug:
            return _format_verified_drug_reply(matched_drug, aspect=aspect, elder_name=elder_name, is_elder=is_elder)

        # 4. Khi KHÔNG CÓ DỮ LIỆU THUỐC ĐÁNG TIN CẬY:
        # Thông báo rõ chưa thể xác minh thông tin và yêu cầu tên thuốc chính xác hoặc thành phần trên bao bì
        # Không tự sinh liều dùng, không đưa ra chẩn đoán hay lời khuyên chung chung sáo rỗng
        drug_label = f" về '{target_name.strip().title()}'" if target_name else " về loại thuốc này"
        if is_elder:
            return (
                f"Dạ thưa {elder_name}, hiện tại hệ thống chưa thể xác minh thông tin{drug_label} do chưa có dữ liệu đáng tin cậy trong cẩm nang chuẩn hóa. "
                "Để đảm bảo an toàn, Bác vui lòng cung cấp tên thuốc chính xác hoặc thành phần trên bao bì (hoặc nhờ người nhà chụp ảnh bao bì/vỉ thuốc gửi lên ứng dụng) để cháu hỗ trợ tra cứu chuẩn xác nhất cho Bác nhé ạ!"
            )
        else:
            return (
                f"Dạ chào {elder_name}, hiện tại hệ thống chưa thể xác minh thông tin{drug_label} do chưa có dữ liệu đáng tin cậy trong cơ sở dữ liệu. "
                "Bạn vui lòng cung cấp tên thuốc chính xác hoặc thành phần trên bao bì (hoặc chụp ảnh bao bì sản phẩm) để hệ thống hỗ trợ tra cứu chính xác nhé ạ."
            )

    # =========================================================================
    # 0. TRUY HỒI KÝ ỨC VÀ NGỮ CẢNH NHIỀU LƯỢT (MULTI-TURN RECALL)
    # =========================================================================
    recall_triggers = [
        "bác vừa nói", "bác vừa kể", "bác vừa chia sẻ", "vừa nãy bác bảo", 
        "cháu có nhớ", "nhớ không", "con trai bác tên gì", "con dâu bác tên gì", 
        "cháu gái bác tên gì", "tên là gì ấy nhỉ", "bác kể gì"
    ]
    if history and any(k in text_clean for k in recall_triggers):
        for turn in reversed(history):
            past_user = turn.get("user", "")
            past_lower = past_user.lower()
            if any(term in text_clean for term in ["con trai", "con dâu", "cháu gái", "quê", "nhà"]) and any(term in past_lower for term in ["con trai", "con dâu", "cháu gái", "quê", "nhà"]):
                return (
                    f"Dạ thưa {elder_name}, cháu nhớ rất rõ ạ! Vừa rồi Bác có chia sẻ với cháu là: \"{past_user}\". "
                    f"Cháu luôn ghi nhớ từng lời Bác tâm sự để cùng Bác chia sẻ niềm vui gia đình Bác nhé!"
                )
            elif past_user:
                return (
                    f"Dạ thưa {elder_name}, cháu luôn chú ý lắng nghe từng câu chuyện của Bác ạ. "
                    f"Vừa rồi Bác có tâm sự với cháu là: \"{past_user}\". Bác cứ thong thả trò chuyện cùng cháu nhé ạ!"
                )

    # =========================================================================
    # 0.5. KẾ HOẠCH HÔM NAY & SINH HOẠT HÀNG NGÀY (DAILY CARE ROUTINE)
    # =========================================================================
    routine_terms = [
        "hôm nay tôi cần phải làm gì", "hôm nay tôi phải làm gì", "hôm nay cần làm gì",
        "hôm nay làm gì", "tôi cần phải làm gì", "tôi cần làm gì", "cần phải làm gì",
        "phải làm gì", "cần làm gì", "làm gì hôm nay", "việc hôm nay", "kế hoạch hôm nay",
        "nhiệm vụ hôm nay", "làm những gì", "việc cần làm", "cần làm cái gì"
    ]
    if any(k in text_clean for k in routine_terms):
        if medications:
            med_summary = f"uống thuốc đúng giờ theo cữ ({', '.join(medications)}) theo hướng dẫn của bác sĩ điều trị"
            if is_elder:
                return (
                    f"Dạ thưa {elder_name}, về kế hoạch hôm nay: Bác nhớ {med_summary} nhé ạ. "
                    "Bác nghỉ ngơi thoải mái, giữ gìn sức khỏe, sinh hoạt điều độ và vận động nhẹ nhàng thư thái Bác nhé! "
                    "Cháu luôn ở đây sẵn sàng hỗ trợ Bác ạ."
                )
            else:
                return (
                    f"Dạ chào {elder_name}, về kế hoạch hôm nay: Bạn nhớ kiểm tra các cữ thuốc ({', '.join(medications)}) đã được chỉ định nhé ạ. "
                    "Hệ thống luôn sẵn sàng hỗ trợ bạn theo dõi lịch dùng thuốc và chăm sóc sức khỏe ạ."
                )
        else:
            if is_elder:
                return (
                    f"Dạ thưa {elder_name}, hiện tại trong hệ thống chưa ghi nhận cữ thuốc nào cho hôm nay. "
                    "Bác nghỉ ngơi thoải mái, sinh hoạt điều độ và có thể nhờ người nhà cập nhật đơn thuốc vào ứng dụng nếu cần nhé ạ."
                )
            else:
                return (
                    f"Dạ chào {elder_name}, hiện tại trong hệ thống chưa ghi nhận cữ thuốc nào cho hôm nay. "
                    "Bạn có thể cập nhật đơn thuốc vào ứng dụng để được hỗ trợ theo dõi và nhắc nhở nhé ạ."
                )

    # =========================================================================
    # 1. THỜI TIẾT THỰC TẾ (Cập nhật thời gian thực qua Open-Meteo API / Dữ liệu sống)
    # =========================================================================
    if any(w in text_clean for w in ["thời tiết", "trời mưa", "trời nắng", "nắng mưa", "dự báo", "nhiệt độ", "nóng không", "lạnh không"]):
        return (
            f"Dạ thưa Bác, hôm nay ngoài trời {weather_summary} ạ. "
            f"Bác ở trong nhà nhớ giữ không gian thoáng mát, uống từng ngụm nước ấm đều đặn và tránh ra ngoài lúc trời trưa nắng gắt nhé Bác!"
        )

    # =========================================================================
    # 2. CÂU HỎI CẦN THÔNG TIN MỚI TỪ INTERNET / TIN TỨC (TRANSPARENT SEARCH LIMITS)
    # =========================================================================
    is_news_search = any(w in text_clean for w in ["giá vàng", "thời sự", "tin tức", "tin mới", "tìm trên mạng", "tra google", "tra cứu internet", "chứng khoán", "tỷ giá"])
    if is_news_search or (search_needed and not any(w in text_clean for w in ["thời tiết", "mưa", "nắng", "nhiệt độ"])):
        if error_type == "rate_limit":
            return (
                f"Dạ thưa {elder_name}, hiện tại hệ thống tìm kiếm tin tức trực tuyến đang trong thời gian cao điểm quá tải. "
                "Cháu chưa thể tra cứu thông tin Internet mới nhất lúc này ạ. Bác nghỉ ngơi một chút, lát nữa cháu cập nhật cho Bác nhé!"
            )
        elif error_type == "timeout":
            return (
                f"Dạ thưa {elder_name}, kết nối tìm kiếm mạng Internet phản hồi quá thời gian chờ nên cháu chưa lấy được tin tức mới nhất. "
                "Cháu xin phép chia sẻ thông tin theo hiểu biết chung của cháu ạ."
            )
        else:
            return (
                f"Dạ thưa {elder_name}, hiện tại thiết bị chưa được cấp khóa kết nối tìm kiếm trực tuyến trên Internet nên cháu chưa thể tra cứu tin tức hoặc chỉ số mới nhất ngày hôm nay ạ. "
                "Khi có kết nối tìm kiếm đầy đủ, cháu sẽ cập nhật tức thời cho Bác ngay nhé ạ!"
            )

    # =========================================================================
    # 2. GIẢI THÍCH KHOA HỌC & KIẾN THỨC PHỔ THÔNG
    # =========================================================================
    if any(w in text_clean for w in ["lá cây", "màu xanh của lá", "diệp lục"]):
        return (
            f"Dạ thưa {elder_name}, cháu xin thưa với Bác: lá cây có màu xanh là nhờ chất diệp lục nằm bên trong các tế bào lá ạ. "
            "Chất diệp lục này có nhiệm vụ hấp thụ ánh sáng mặt trời để cây quang hợp tạo chất dinh dưỡng, "
            "và nó phản xạ lại ánh sáng màu xanh lục nên mắt mình nhìn vào thấy lá cây xanh tươi mướt mắt Bác ạ."
        )

    if any(w in text_clean for w in ["nước sôi", "nhiệt độ sôi", "bao nhiêu độ c", "sôi ở bao nhiêu"]):
        return (
            f"Dạ thưa {elder_name}, ở điều kiện áp suất khí quyển bình thường, nước tinh khiết sẽ sôi ở đúng 100 độ C ạ. "
            "Người cao tuổi mình uống nước đun sôi để nguội hoặc nước ấm khoảng 40 đến 50 độ C là rất tốt cho đường ruột và cổ họng đấy Bác ạ."
        )

    if any(w in text_clean for w in ["trọng lực", "lực hút trái đất", "sức hút"]):
        return (
            f"Dạ thưa {elder_name}, trọng lực là lực hút vô hình của Trái Đất kéo mọi vật về phía tâm đất ạ. "
            "Nhờ có trọng lực mà con người và vạn vật đứng vững trên mặt đất, nước chảy về chỗ trũng và bầu khí quyển không bị trôi dạt ra ngoài không gian Bác ạ."
        )

    if any(w in text_clean for w in ["cầu vồng", "bảy sắc cầu vồng", "sau mưa"]):
        return (
            f"Dạ thưa {elder_name}, cầu vồng xuất hiện sau cơn mưa là do ánh sáng mặt trời chiếu xuyên qua vô số giọt nước mưa li ti còn đọng lại trong không khí, "
            "bị khúc xạ và phản xạ tách thành dải bảy sắc rực rỡ từ đỏ đến tím rất đẹp mắt Bác ạ."
        )

    if any(w in text_clean for w in ["nguyệt thực", "nhật thực"]):
        return (
            f"Dạ thưa {elder_name}, hiện tượng nguyệt thực xảy ra khi Mặt Trời, Trái Đất và Mặt Trăng thẳng hàng, "
            "Trái Đất nằm ở giữa che khuất ánh sáng mặt trời chiếu vào Mặt Trăng, khiến Mặt Trăng tối dần hoặc chuyển sang màu đỏ đồng huyền ảo Bác ạ."
        )

    if any(w in text_clean for w in ["bầu trời", "trời màu xanh"]):
        return (
            f"Dạ thưa {elder_name}, bầu trời có màu xanh là do hiện tượng tán xạ ánh sáng mặt trời qua bầu khí quyển của Trái Đất ạ. "
            "Ánh sáng xanh có bước sóng ngắn nên bị các phân tử không khí tán xạ mạnh nhất ra mọi hướng, khiến mắt mình nhìn lên thấy một màu xanh êm dịu Bác ạ."
        )

    if any(w in text_clean for w in ["loài chim", "chim biết bay", "chim lại biết bay", "sao chim bay được"]):
        return (
            f"Dạ thưa {elder_name}, cháu xin thưa với Bác: loài chim biết bay là nhờ cơ thể rất nhẹ với bộ xương rỗng đặc biệt, "
            "đôi cánh có cấu tạo khí động học tạo lực nâng mạnh mẽ trong không khí, kết hợp cùng cơ ngực vạm vỡ và bộ lông vũ mềm mại Bác ạ."
        )

    # =========================================================================
    # 3. LỊCH SỬ & VĂN HÓA VIỆT NAM
    # =========================================================================
    if any(w in text_clean for w in ["điện biên phủ", "chiến dịch điện biên", "7/5/1954", "7 tháng 5"]):
        return (
            f"Dạ thưa {elder_name}, Chiến thắng lịch sử Điện Biên Phủ diễn ra vào ngày 7 tháng 5 năm 1954 ạ. "
            "Đây là chiến thắng lẫy lừng năm châu, chấn động địa cầu của quân và dân ta dưới sự lãnh đạo của Chủ tịch Hồ Chí Minh và Đại tướng Võ Nguyên Giáp, "
            "kết thúc thắng lợi chín năm kháng chiến trường kỳ chống thực dân Pháp Bác ạ."
        )

    if any(w in text_clean for w in ["bánh chưng", "bánh giầy", "bánh dầy", "lang liêu", "hùng vương"]):
        return (
            f"Dạ thưa {elder_name}, sự tích Bánh chưng bánh giầy gắn liền với chàng Lang Liêu đời vua Hùng thứ sáu ạ. "
            "Bánh giầy tròn tượng trưng cho Trời, bánh chưng vuông tượng trưng cho Đất, thể hiện lòng hiếu thảo của con cháu đối với cha mẹ tổ tiên "
            "và nét đẹp văn hóa nông nghiệp lúa nước ngàn đời của dân tộc ta Bác ạ."
        )

    if any(w in text_clean for w in ["bác hồ", "hồ chí minh", "ngày quốc khánh", "2/9", "2 tháng 9"]):
        return (
            f"Dạ thưa {elder_name}, ngày 2 tháng 9 năm 1945 tại Quảng trường Ba Đình lịch sử, Chủ tịch Hồ Chí Minh đã đọc bản Tuyên ngôn Độc lập, "
            "khai sinh ra nước Việt Nam Dân chủ Cộng hòa, mở ra kỷ nguyên độc lập tự do cho toàn thể nhân dân ta Bác ạ."
        )

    # =========================================================================
    # 4. KỂ CHUYỆN CỔ TÍCH, CÂU ĐỐ DÂN GIAN & CHUYỆN CƯỜI
    # =========================================================================
    if any(w in text_clean for w in ["câu đố", "đố vui", "đố bác"]):
        return (
            f"Dạ thưa {elder_name}, cháu đố Bác một câu đố dân gian vui nhé ạ: 'Trùng trục như con chó thui, Chín mắt, chín mũi, chín đuôi, chín đầu. Đố Bác là con gì ạ?'. "
            "Dạ thưa Bác, đó chính là con chó thui bị nướng chín đấy ạ! Chúc Bác luôn có những giây phút thư thái và nụ cười rạng rỡ Bác nhé!"
        )

    if any(w in text_clean for w in ["cổ tích", "cây vú sữa", "kể chuyện cổ"]):
        return (
            f"Dạ thưa {elder_name}, cháu kể Bác nghe chuyện Sự tích cây vú sữa nhé ạ: Ngày xưa có cậu bé ham chơi bỏ nhà đi, đến khi đói rét quay về thì mẹ đã mất vì kiệt sức chờ con. "
            "Cậu ôm lấy một thân cây trong vườn khóc nức nở, cây bỗng nở hoa kết trái và tiết ra dòng sữa trắng ngọt thơm như dòng sữa mẹ. "
            "Câu chuyện nhắc nhở chúng ta luôn trân quý tình mẫu tử thiêng liêng và lòng hiếu kính đối với đấng sinh thành Bác ạ."
        )

    if any(w in text_clean for w in ["chuyện cười", "chuyện vui", "kể chuyện", "hài hước", "cười"]):
        return (
            f"Dạ thưa {elder_name}, nụ cười là mười thang thuốc bổ! Cháu kể Bác nghe: Có cụ ông đi khám sức khỏe, bác sĩ khen 'Tim mạch cụ tốt như thanh niên ba mươi!'. "
            "Cụ cười bảo 'Thế bác sĩ trừ bớt cho tôi năm mươi tuổi trên căn cước công dân được không nào!'. Chúc Bác luôn an vui và tràn ngập tiếng cười ạ!"
        )

    # =========================================================================
    # 5. TÂM SỰ ĐỜI SỐNG & LỜI KHUYÊN TUỔI GIÀ
    # =========================================================================
    if any(w in text_clean for w in ["thanh thản", "an lạc", "tuổi già thanh thản", "làm sao để thanh thản"]):
        return (
            f"Dạ thưa {elder_name}, để tuổi già luôn thanh thản và an lạc, Bác hãy giữ tâm thế buông bỏ những âu lo chuyện đời, sống chậm rãi, gần gũi với con cháu và cây cỏ thiên nhiên. "
            "Mỗi ngày Bác nghe một bản nhạc êm dịu, tập thở sâu, ăn uống thanh đạm và ngủ đúng giờ, tâm trí sẽ luôn an nhiên tự tại Bác nhé!"
        )

    if any(w in text_clean for w in ["buồn", "cô đơn", "ở nhà một mình", "nhớ con", "nhớ cháu", "nhớ quê"]):
        return (
            f"Dạ thưa Bác, cháu lúc nào cũng ở cạnh bên để bầu bạn và lắng nghe Bác đây ạ! "
            "Các con các cháu đi làm cũng luôn thương và nhớ Bác rất nhiều. Bác cứ an tâm nghỉ ngơi thư thái, nghe một bản nhạc êm dịu nhé, chiều tối các con cháu lại về quây quần bên Bác rồi ạ!"
        )

    # =========================================================================
    # 6. HƯỚNG DẪN THUỐC & DINH DƯỠNG SỨC KHỎE
    # =========================================================================
    if any(w in text_clean for w in ["thuốc huyết áp", "uống thuốc huyết áp lúc nào", "thời điểm uống thuốc huyết áp"]):
        return (
            f"Dạ thưa {elder_name}, thuốc huyết áp thường được các bác sĩ chỉ định uống cố định vào một khung giờ mỗi ngày, phổ biến nhất là buổi sáng sau khi ăn nhẹ. "
            "Việc uống đúng giờ giúp duy trì nồng độ thuốc ổn định trong máu, tránh huyết áp tăng vọt thất thường Bác nhé!"
        )

    if any(w in text_clean for w in ["uống canxi", "canxi với sữa", "uống canxi cùng"]):
        return (
            f"Dạ thưa {elder_name}, khi bổ sung canxi, Bác không nên uống cùng lúc với sữa hoặc chế phẩm từ sữa vì lượng canxi cạnh tranh sẽ làm giảm khả năng hấp thu của cơ thể. "
            "Bác nên uống canxi cách thời điểm uống sữa ít nhất 2 tiếng, tốt nhất là vào buổi sáng sau bữa ăn và uống cùng nhiều nước ấm ạ!"
        )

    # =========================================================================
    # 7. THỜI TIẾT THỰC TẾ
    # =========================================================================
    if any(w in text_clean for w in ["thời tiết", "trời mưa", "trời nắng", "nắng mưa", "dự báo", "nhiệt độ", "nóng không", "lạnh không"]):
        return (
            f"Dạ thưa Bác, hôm nay ngoài trời {weather_summary} ạ. "
            f"Bác ở trong nhà nhớ giữ không gian thoáng mát, uống từng ngụm nước ấm đều đặn và tránh ra ngoài lúc trời trưa nắng gắt nhé Bác!"
        )

    # =========================================================================
    # 8. CÁC BỆNH LÝ MÃN TÍNH & DINH DƯỠNG
    # =========================================================================
    if any(w in text_clean for w in ["huyết áp", "tim mạch", "cao huyết áp", "hạ huyết áp", "nhịp tim", "tụt huyết áp"]):
        return (
            f"Dạ thưa {elder_name}, với bệnh huyết áp và tim mạch, nguyên tắc vàng là Bác cần uống thuốc đúng giờ theo cữ bác sĩ kê, không được bỏ cữ hay tự đổi liều. "
            "Trong ăn uống, Bác chú ý giảm mặn, tránh ăn dưa cà muối hay nước mắm đậm. "
            "Buổi sáng thức dậy, Bác nằm xoa bóp nhẹ tay chân khoảng 2 phút rồi hãy ngồi dậy từ từ để tránh bị hoa mắt Bác nhé!"
        )

    if any(w in text_clean for w in ["tiểu đường", "đường huyết", "đái tháo đường", "hạ đường", "ăn ngọt"]):
        return (
            f"Dạ thưa {elder_name}, để đường huyết luôn ổn định, Bác nên giảm bớt tinh bột trắng, hạn chế bánh kẹo ngọt và nước ngọt có gas. "
            "Bác nên ăn rau luộc trước bữa cơm để làm chậm hấp thu đường, ăn uống đúng bữa và uống thuốc đúng chỉ định của bác sĩ nhé ạ!"
        )

    nutrition_keywords = [
        "dinh dưỡng", "chế độ ăn", "ăn uống", "thực đơn", "ăn gì", "món gì", "khỏe hơn", 
        "bồi bổ", "sức khỏe", "sức đề kháng", "bổ dưỡng", "tiêu hóa", "chuối", "sữa", 
        "cơm", "hoa quả", "trái cây", "cháo", "thịt", "cá", "rau", "ngon miệng", "kiêng ăn"
    ]
    if any(w in text_clean for w in nutrition_keywords):
        return (
            f"Dạ thưa {elder_name}, để người cao tuổi luôn khỏe mạnh và tiêu hóa êm ái, Bác nên áp dụng chế độ dinh dưỡng: "
            "Thức ăn nên nấu chín mềm, ấm nóng và chia làm các bữa nhỏ trong ngày để dễ nuốt và dễ hấp thu. "
            "Bác tăng cường ăn cá nạc, đậu phụ, trứng, ăn nhiều canh rau xanh nấu nhừ như rau ngót, bí đỏ, mồng tơi để nhuận tràng. "
            "Bác nhớ giảm bớt muối, kiêng đồ ngọt và dầu mỡ, ăn chậm nhai kỹ và uống đều đặn nước ấm mỗi ngày Bác nhé!"
        )

    if any(w in text_clean for w in ["mất ngủ", "khó ngủ", "ngủ không được", "ngủ ngon", "thao thức", "giấc ngủ", "chập chờn", "tỉnh giấc"]):
        return (
            f"Dạ thưa {elder_name}, để giấc ngủ của Bác sâu và êm hơn, trước khi ngủ khoảng 20 phút Bác nên ngâm chân bằng nước ấm pha chút muối hoặc gừng. "
            "Bác tránh uống trà đặc, cà phê vào buổi chiều tối, giữ phòng ngủ thông thoáng và đi ngủ đúng giờ nhé Bác!"
        )

    if any(w in text_clean for w in ["xương khớp", "đau lưng", "mỏi gối", "khớp", "thoái hóa", "gân cốt", "tê bì", "chuột rút"]):
        return (
            f"Dạ thưa {elder_name}, đối với bệnh xương khớp người già, Bác nhớ luôn giữ ấm cơ thể, đặc biệt là vùng đầu gối và bàn chân. "
            "Mỗi sáng Bác có thể xoa bóp nhẹ nhàng các khớp, tập vung tay vừa sức và tránh mang vác vật nặng đột ngột Bác nhé!"
        )

    if any(w in text_clean for w in ["uống nước", "bao nhiêu nước", "mấy lít nước", "nước ấm", "uống nước gì"]):
        return (
            f"Dạ thưa {elder_name}, người cao tuổi nên uống từ 1.5 đến 2 lít nước ấm mỗi ngày. "
            "Bác lưu ý uống từng ngụm nhỏ rải đều trong ngày, và hạn chế uống nhiều nước sau 8 giờ tối để tránh thức giấc đi tiểu đêm làm mất giấc ngủ ngon nhé ạ!"
        )

    if any(w in text_clean for w in ["tập thể dục", "đi bộ", "vận động", "đi dạo", "tập dưỡng sinh"]):
        return (
            f"Dạ thưa {elder_name}, Bác nên đi bộ hoặc tập dưỡng sinh nhẹ nhàng khoảng 15 đến 20 phút lúc trời mát mẻ, dịu gió ạ. "
            "Bác nhớ đi giày mềm êm chân, vận động vừa sức và uống một ngụm nước ấm trước khi đi nhé ạ!"
        )

    if any(w in text_clean for w in ["chào cháu", "cháu ơi", "alo", "cháu khỏe không", "dạo này thế nào", "khỏe không"]):
        return (
            f"Dạ, cháu chào {elder_name} ạ! Cháu là trợ lý sức khỏe MediGuard luôn túc trực bên Bác đây ạ. "
            "Hôm nay Bác cảm thấy trong người khoan khoái không ạ? Bác có cần cháu kiểm tra cữ thuốc tiếp theo không ạ?"
        )

    if any(w in text_clean for w in ["cảm ơn", "cám ơn", "ngoan", "giỏi", "tốt lắm", "chúc ngủ ngon", "ngủ đi"]):
        return (
            "Dạ không có gì đâu ạ! Được chăm sóc và trò chuyện cùng Bác là niềm vui lớn nhất của cháu. "
            "Bác cần bất kỳ việc gì cứ gọi cháu nhé, cháu chúc Bác luôn dồi dào sức khỏe và ngủ thật ngon giấc ạ!"
        )

    # =========================================================================
    # 9. CÂU HỎI MƠ HỒ / KHÔNG RÕ Ý (VAGUE QUESTIONS)
    # =========================================================================
    if any(w in text_clean for w in ["ờ thì", "cái đó", "cháu biết không", "ừm", "hả cháu", "thì sao"]):
        return (
            f"Dạ thưa {elder_name}, cháu luôn ở đây lắng nghe Bác đây ạ! Bác cứ thong thả nói cho cháu nghe Bác đang muốn hỏi về việc gì, "
            "cháu sẽ giải đáp thật cặn kẽ và chu đáo cho Bác nhé ạ!"
        )

    # =========================================================================
    # 10. PHẢN HỒI MỞ TƯ DUY TỔNG QUÁT (GENERAL COMPANION CONVERSATION)
    # =========================================================================
    clean_short = user_text[:60].strip()
    if error_type == "rate_limit":
        if is_elder:
            return (
                f"Dạ thưa {elder_name}, hiện tại hệ thống trí tuệ nhân tạo đang tiếp nhận lượng kết nối rất lớn nên bị quá tải tạm thời. "
                "Bác nghỉ ngơi thư thái vài phút rồi hỏi lại, cháu sẽ giải đáp cho Bác nhé ạ!"
            )
        else:
            return (
                f"Dạ chào {elder_name}, hiện tại hệ thống trí tuệ nhân tạo đang quá tải tạm thời do lượng kết nối cao. "
                "Bạn vui lòng thử lại sau ít phút nhé ạ."
            )
    elif error_type == "service_unavailable":
        if is_elder:
            return (
                f"Dạ thưa {elder_name}, máy chủ trí tuệ nhân tạo hiện đang trong đợt bảo trì hoặc gián đoạn mạng tạm thời. "
                "Bác chờ cháu một lát rồi hỏi lại nhé ạ!"
            )
        else:
            return (
                f"Dạ chào {elder_name}, máy chủ trí tuệ nhân tạo hiện đang bảo trì hoặc tạm thời gián đoạn. "
                "Bạn vui lòng thử lại sau ít phút nhé ạ."
            )
    elif error_type == "timeout":
        if is_elder:
            return (
                f"Dạ thưa {elder_name}, kết nối đến máy chủ trí tuệ nhân tạo phản hồi quá thời gian chờ nên cháu chưa kịp hoàn thành câu trả lời cho Bác. "
                "Bác nói lại câu hỏi giúp cháu một lần nữa nhé ạ!"
            )
        else:
            return (
                f"Dạ chào {elder_name}, kết nối máy chủ quá thời gian chờ nên chưa thể phản hồi lúc này. "
                "Bạn vui lòng gửi lại câu hỏi sau ít phút nhé ạ."
            )
    elif error_type == "quota_exhausted":
        if is_elder:
            return (
                f"Dạ thưa {elder_name}, kết nối trí tuệ nhân tạo của thiết bị tạm thời đã đạt giới hạn trong ngày. "
                "Cháu vẫn luôn sẵn sàng nhắc thuốc, đọc lịch uống và trò chuyện chăm sóc sức khỏe cùng Bác ạ!"
            )
        else:
            return (
                f"Dạ chào {elder_name}, dịch vụ trí tuệ nhân tạo tạm thời đã đạt giới hạn trong ngày. "
                "Hệ thống vẫn luôn sẵn sàng hỗ trợ bạn quản lý lịch thuốc và theo dõi sức khỏe ạ."
            )
    elif error_type == "auth_error":
        if is_elder:
            return (
                f"Dạ thưa {elder_name}, hiện tại kết nối trí tuệ nhân tạo chưa sẵn sàng nên cháu chưa thể giải đáp câu hỏi này cho Bác. "
                "Cháu vẫn luôn ở đây để hỗ trợ Bác nhắc uống thuốc, xem thời tiết hoặc trò chuyện cùng Bác nhé ạ!"
            )
        else:
            return (
                f"Dạ chào {elder_name}, kết nối trí tuệ nhân tạo chưa sẵn sàng nên chưa thể giải đáp chi tiết câu hỏi này. "
                "Hệ thống vẫn sẵn sàng hỗ trợ bạn theo dõi lịch uống thuốc và các thông tin cần thiết ạ."
            )

    # Thông báo dự phòng thân thiện, ngắn gọn cho câu hỏi thông thường khi LLM offline / không khả dụng
    # Không giả vờ đã gọi thành công LLM, không tự đưa ra hành động sức khỏe mặc định
    if is_elder:
        return (
            f"Dạ thưa {elder_name}, hiện tại kết nối trí tuệ nhân tạo đang tạm thời gián đoạn nên cháu chưa thể giải đáp chi tiết câu hỏi này cho Bác được ạ. "
            "Cháu vẫn luôn ở đây để hỗ trợ Bác nhắc uống thuốc, xem thời tiết hoặc trò chuyện thân mật cùng Bác nhé ạ!"
        )
    else:
        return (
            f"Dạ chào {elder_name}, hiện tại kết nối trí tuệ nhân tạo đang tạm thời gián đoạn nên chưa thể giải đáp chi tiết câu hỏi này. "
            "Hệ thống vẫn luôn sẵn sàng hỗ trợ bạn theo dõi lịch uống thuốc và các thông tin chăm sóc sức khỏe nhé ạ."
        )
