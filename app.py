from pathlib import Path
from datetime import date, datetime, timezone, timedelta
import json

import streamlit as st
import streamlit.components.v1 as components
import requests
from requests.auth import HTTPBasicAuth

st.set_page_config(
    page_title="BPM Team Project Management Dashboard",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# 隱藏 Streamlit 預設的 header/footer，讓 dashboard 滿版呈現
st.markdown(
    """
    <style>
      #MainMenu, header, footer {visibility: hidden;}
      .block-container {padding: 0.6rem 1rem 0 !important; max-width: 100% !important;}
      iframe {display: block; width: 100%; border: none;}
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Jira 連線設定（存在 Streamlit secrets 裡，不要寫死在程式碼）──
# .streamlit/secrets.toml 需要：
#   JIRA_DOMAIN = "alp-bpmteam-dashboard.atlassian.net"
#   JIRA_EMAIL = "harry.yang@alp.global"
#   JIRA_API_TOKEN = "..."   (Jira 帳號設定 -> Security -> API tokens 建立)
#   GEMINI_API_KEY = "..."   (Dashboard 小幫手用)
JIRA_DOMAIN = st.secrets["JIRA_DOMAIN"]
JIRA_EMAIL = st.secrets["JIRA_EMAIL"]
JIRA_API_TOKEN = st.secrets["JIRA_API_TOKEN"]
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")

JIRA_BASE = f"https://{JIRA_DOMAIN}/rest/api/3"
AUTH = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)
HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}

# Jira site 上的範例／示範專案，掃描全部專案時排除
EXCLUDE_PROJECT_KEYS = {"SAM1", "KAN"}

# 重要：不同專案即使欄位「顯示名稱」相同（例如都叫「結束日期」），
# Jira 背後產生的 customfield ID 可能完全不同（實測發現 BPM 用 customfield_10048，
# AHP 卻是 customfield_10137）。原因是在 Team-managed 專案加欄位時，
# 就算打一樣的名字，Jira 也可能建立一個全新的自訂欄位，而不是重複使用既有的。
# 因此不能把 ID 寫死，必須在執行時依「顯示名稱」動態查詢每個專案實際對應的 ID。
# 這個機制涵蓋所有會因專案不同而 ID 不同的欄位（包含負責人、結束日期、進度說明...），
# 讀取跟「小幫手」寫入都走同一套，不會再有「這個專案讀得到、那個專案讀不到」的落差。
FIELD_DISPLAY_NAMES = {
    "start": "開始日期",
    "end": "結束日期",
    "actual_end": "實際完成日",
    "owner": "負責人",
    "decide": "須優先決議",
    "note": "決議事項說明",
    "prog_note": "進度說明",
    "priority_custom": "優先順序",
}

STATUS_TRANSITION = {"未開始": "2", "進行中": "3", "已完成": "5"}

# ── 初始化 session state（Dashboard 小幫手用）──
if "show_chat" not in st.session_state:
    st.session_state.show_chat = False
if "ad_msg" not in st.session_state:
    st.session_state.ad_msg = []
if "ad_hist" not in st.session_state:
    st.session_state.ad_hist = []
if "pending_update" not in st.session_state:
    st.session_state.pending_update = None
if "weekly_report" not in st.session_state:
    st.session_state.weekly_report = None

# ── 按鈕列 ──
btn_area, _ = st.columns([1, 3])
with btn_area:
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        if st.button(
            "💬 Dashboard 小幫手",
            type="primary" if st.session_state.show_chat else "secondary",
            use_container_width=True,
        ):
            st.session_state.show_chat = not st.session_state.show_chat
            st.rerun()
    with c2:
        if st.button("🔄 更新資料", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
    with c3:
        gen_report_clicked = st.button("📄 產生週報", use_container_width=True)


def _doc_to_text(v):
    """把 Jira 欄位值轉成純文字，相容三種常見格式：
    1. 純字串
    2. 段落文字欄位（ADF doc，如 {"type":"doc","content":[...]}）
    3. 選單/下拉選項欄位（Select List，如 {"value":"已決議","id":"..."}）
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        # 選單類型欄位：直接有 "value" 這個 key，沒有 "content"
        if "value" in v and "content" not in v:
            return v.get("value") or ""
        out = []
        for c in v.get("content", []):
            if c.get("type") == "paragraph":
                for t in c.get("content", []):
                    if t.get("type") == "text":
                        out.append(t.get("text", ""))
            elif c.get("type") == "text":
                out.append(c.get("text", ""))
        return "".join(out)
    return ""


def _to_date(d):
    if not d:
        return None
    try:
        return datetime.fromisoformat(str(d)[:10]).date()
    except ValueError:
        return None


def calc_overdue_days(status: str, end_date, actual_date=None, today=None) -> int:
    """逾期天數，對應原本 Notion 公式的邏輯：
    - 進行中：今天 相對 結束日期，逾期才計數，否則 0
    - 已完成：實際完成日 相對 結束日期，晚於預期才計數，否則 0
    - 其他狀態（未開始等）：0
    """
    today = today or date.today()
    end_d = _to_date(end_date)
    actual_d = _to_date(actual_date)

    if status == "進行中":
        if end_d is None:
            return 0
        diff = (today - end_d).days
        return diff if diff > 0 else 0

    if status == "已完成":
        if actual_d is None or end_d is None:
            return 0
        diff = (actual_d - end_d).days
        return diff if diff > 0 else 0

    return 0


@st.cache_data(ttl=300)
def fetch_projects():
    """列出這個 Jira site 上所有專案，排除範例／示範專案。"""
    res = requests.get(
        f"{JIRA_BASE}/project/search",
        auth=AUTH, headers=HEADERS, params={"maxResults": 100},
    )
    res.raise_for_status()
    values = res.json().get("values", [])
    return [p for p in values if p["key"] not in EXCLUDE_PROJECT_KEYS]


@st.cache_data(ttl=300)
def get_project_field_map(proj_key: str):
    """依「顯示名稱」動態解析這個專案實際使用的 customfield ID。

    做法：先抓這個專案任一筆 issue 的 key，再用
    GET /rest/api/3/issue/{key}?expand=names 取得「欄位ID -> 顯示名稱」的對照，
    反查出我們關心的幾個欄位（結束日期、負責人...）在這個專案裡對應的實際 ID。

    注意：官方文件說 /search/jql 的 expand=names 也能拿到這個對照表，
    但實測（含 Atlassian 社群回報）目前這個參數在新版搜尋 API 上是壞的、回傳空值，
    所以改用單筆 issue 的 expand=names，這個是有效的。
    """
    res = requests.get(
        f"{JIRA_BASE}/search/jql",
        auth=AUTH, headers=HEADERS,
        params={
            "jql": f'project = "{proj_key}" ORDER BY created ASC',
            "fields": "summary",
            "maxResults": 1,
        },
    )
    res.raise_for_status()
    issues = res.json().get("issues", [])
    if not issues:
        return {}

    sample_key = issues[0]["key"]
    res2 = requests.get(
        f"{JIRA_BASE}/issue/{sample_key}",
        auth=AUTH, headers=HEADERS, params={"expand": "names"},
    )
    res2.raise_for_status()
    names = res2.json().get("names", {})  # field_id -> 顯示名稱

    display_to_id = {}
    for field_id, display_name in names.items():
        if display_name in FIELD_DISPLAY_NAMES.values() and display_name not in display_to_id:
            display_to_id[display_name] = field_id

    # 轉成用我們自己好記的 key（start/end/owner...）對應到這個專案實際的 field_id
    return {key: display_to_id[name] for key, name in FIELD_DISPLAY_NAMES.items() if name in display_to_id}


@st.cache_data(ttl=300)
def fetch_all_tasks():
    tasks = []
    errors = []

    for proj in fetch_projects():
        proj_key = proj["key"]
        proj_name = proj["name"]
        try:
            field_map = get_project_field_map(proj_key)
            wanted_field_ids = list(set(field_map.values()))
            request_fields = ["summary", "status", "priority"] + wanted_field_ids

            next_page_token = None
            while True:
                params = {
                    "jql": f'project = "{proj_key}" ORDER BY created ASC',
                    "fields": ",".join(request_fields),
                    "maxResults": 100,
                }
                if next_page_token:
                    params["nextPageToken"] = next_page_token

                # 注意：舊版 /rest/api/3/search 已被 Atlassian 下架（2025年起回傳 410 Gone），
                # 這裡改用新版 /rest/api/3/search/jql，分頁方式也從 startAt/total 換成 nextPageToken/isLast。
                res = requests.get(
                    f"{JIRA_BASE}/search/jql",
                    auth=AUTH, headers=HEADERS, params=params,
                )
                res.raise_for_status()
                data = res.json()
                issues = data.get("issues", [])

                for issue in issues:
                    f = issue["fields"]

                    def get_field(key):
                        fid = field_map.get(key)
                        return f.get(fid) if fid else None

                    status = (f.get("status") or {}).get("name") or "未開始"

                    prio_custom = _doc_to_text(get_field("priority_custom"))
                    prio_native = (f.get("priority") or {}).get("name", "")
                    prio = prio_custom or prio_native

                    end_d = get_field("end")
                    actual_end = get_field("actual_end")

                    progress = 100 if status == "已完成" else (0 if status == "未開始" else 50)

                    tasks.append({
                        "issue_key": issue["key"],
                        "proj": proj_name,
                        "task": f.get("summary", ""),
                        "owner": _doc_to_text(get_field("owner")),
                        "prio": prio,
                        "status": status,
                        "start": get_field("start"),
                        "end": end_d,
                        "progress": progress,
                        "decide": _doc_to_text(get_field("decide")) or "否",
                        "note": _doc_to_text(get_field("note")),
                        "prog_note": _doc_to_text(get_field("prog_note")),
                        "actual_end": actual_end or None,
                        "overdue_days": calc_overdue_days(status, end_d, actual_end),
                    })

                next_page_token = data.get("nextPageToken")
                if data.get("isLast", True) or not next_page_token or not issues:
                    break
        except Exception as e:
            errors.append(f"{proj_name}: {e}")

    return tasks, errors


def update_jira_issue(issue_key: str, updates: dict):
    """updates: dict，key 為欄位顯示名稱（狀態/負責人/結束日/進度說明/優先/須決議），value 為新值。

    這個 issue 屬於哪個專案，就用哪個專案自己的欄位 ID（透過 get_project_field_map
    動態解析），不再寫死 BPM 的 ID——否則對 AHP、VVTE 等其他專案的 issue 下手，
    很可能寫進一個完全不相關的欄位。
    """
    errors = []
    proj_key = issue_key.split("-")[0]
    field_map = get_project_field_map(proj_key)
    fields_payload = {}

    def text_doc(text):
        return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}

    for field, value in updates.items():
        if field == "狀態":
            tid = STATUS_TRANSITION.get(value)
            if tid:
                res = requests.post(
                    f"{JIRA_BASE}/issue/{issue_key}/transitions",
                    auth=AUTH, headers=HEADERS,
                    json={"transition": {"id": tid}},
                )
                if not res.ok:
                    errors.append(f"狀態更新失敗：{res.text}")
        elif field == "負責人":
            fid = field_map.get("owner")
            if not fid:
                errors.append(f"這個專案（{proj_key}）沒有「負責人」欄位，無法更新")
            else:
                res = requests.put(
                    f"{JIRA_BASE}/issue/{issue_key}",
                    auth=AUTH, headers=HEADERS,
                    json={"fields": {fid: text_doc(value)}},
                )
                if not res.ok:
                    errors.append(f"負責人更新失敗：{res.text}")
        elif field == "結束日":
            fid = field_map.get("end")
            if not fid:
                errors.append(f"這個專案（{proj_key}）沒有「結束日期」欄位，無法更新")
            else:
                fields_payload[fid] = value
        elif field == "進度說明":
            fid = field_map.get("prog_note")
            if not fid:
                errors.append(f"這個專案（{proj_key}）沒有「進度說明」欄位，無法更新")
            else:
                fields_payload[fid] = text_doc(value)
        elif field == "優先":
            fid = field_map.get("priority_custom")
            if not fid:
                errors.append(f"這個專案（{proj_key}）沒有「優先順序」自訂欄位，無法更新")
            else:
                fields_payload[fid] = text_doc(value)
        elif field == "須決議":
            fid = field_map.get("decide")
            if not fid:
                errors.append(f"這個專案（{proj_key}）沒有「須優先決議」欄位，無法更新")
            else:
                # 這個欄位目前是「選單/下拉選項」類型，寫入格式跟純文字段落不同，
                # 用 {"value": "已決議"} 這種選項格式，不能再用 text_doc()。
                fields_payload[fid] = {"value": value}

    if fields_payload:
        res = requests.put(
            f"{JIRA_BASE}/issue/{issue_key}",
            auth=AUTH, headers=HEADERS,
            json={"fields": fields_payload},
        )
        if not res.ok:
            errors.append(f"欄位更新失敗：{res.text}")

    return errors


def call_gemini_with_retry(sys_prompt: str, history: list, max_retries: int = 2):
    """呼叫 Gemini API，並加上重試機制、明確的錯誤判斷，避免小幫手「卡住不回應」或
    回傳一個看不懂的原始錯誤訊息（例如單純顯示 KeyError: 'candidates'）。

    回傳 (成功與否: bool, 文字內容或錯誤訊息: str)
    """
    last_err = "未知錯誤"
    for attempt in range(max_retries + 1):
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={GEMINI_API_KEY}",
                json={"system_instruction": {"parts": [{"text": sys_prompt}]}, "contents": history},
                timeout=20,
            )
        except requests.exceptions.Timeout:
            last_err = "Gemini 回應超過 20 秒沒有結果（逾時）"
            continue  # 逾時值得重試一次
        except requests.exceptions.ConnectionError:
            last_err = "無法連線到 Gemini API（網路問題）"
            continue

        # 明確依 HTTP 狀態碼判斷，不要讓後面的 .json() 解析在錯誤情況下丟出看不懂的例外
        if r.status_code == 429:
            last_err = "Gemini API 已達流量上限（429），請稍後再試"
            continue  # 值得重試
        if r.status_code in (401, 403):
            return False, "Gemini API 金鑰無效或權限不足，請檢查 Streamlit secrets 裡的 GEMINI_API_KEY"
        if r.status_code >= 500:
            last_err = f"Gemini 伺服器端錯誤（{r.status_code}）"
            continue  # 伺服器暫時性問題，值得重試
        if r.status_code != 200:
            return False, f"Gemini API 回傳非預期狀態碼：{r.status_code}"

        try:
            data = r.json()
        except ValueError:
            last_err = "Gemini 回傳的內容不是合法的 JSON"
            continue

        candidates = data.get("candidates") or []
        if not candidates:
            # 常見於被安全性過濾器擋下（沒有候選回答）
            block_reason = (data.get("promptFeedback") or {}).get("blockReason")
            if block_reason:
                return False, f"這則訊息被 Gemini 的安全性過濾器擋下（原因：{block_reason}），請換個問法"
            last_err = "Gemini 沒有回傳任何候選回答"
            continue

        finish_reason = candidates[0].get("finishReason")
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        if not parts or "text" not in parts[0]:
            if finish_reason == "SAFETY":
                return False, "這則訊息的回答被安全性過濾器擋下，請換個問法"
            if finish_reason == "MAX_TOKENS":
                last_err = "回答內容被截斷（超過長度上限）"
                continue
            last_err = f"Gemini 回應格式異常（finishReason: {finish_reason}）"
            continue

        return True, parts[0]["text"]

    return False, f"重試 {max_retries} 次後仍失敗：{last_err}"


def compute_stats(tasks: list) -> dict:
    """精確算好各種統計數字，供小幫手回答統計問題、以及自動風險摘要共用。
    這裡算的結果是唯一真相來源，不管是聊天室還是自動摘要都不應該自己另外用 AI 去數。
    """
    proj_names = sorted(set(t.get("proj", "") for t in tasks))
    return {
        "總任務件數": len(tasks),
        "已完成件數": sum(1 for t in tasks if t.get("status") == "已完成"),
        "進行中件數": sum(1 for t in tasks if t.get("status") == "進行中"),
        "未開始件數": sum(1 for t in tasks if t.get("status") == "未開始"),
        "落後任務件數": sum(1 for t in tasks if t.get("status") == "進行中" and (t.get("overdue_days") or 0) > 0),
        "須優先決議件數": sum(1 for t in tasks if t.get("decide") == "待決議"),
        "各專案總任務數": {p: sum(1 for t in tasks if t.get("proj") == p) for p in proj_names},
        "各專案已完成數": {p: sum(1 for t in tasks if t.get("proj") == p and t.get("status") == "已完成") for p in proj_names},
    }


@st.cache_data(ttl=300, show_spinner=False)
def generate_risk_summary(tasks_key: str, tasks: list, stats: dict) -> str:
    """用 Gemini 產生 2-4 條重點風險摘要，不用使用者主動問。
    用 st.cache_data 快取（依 tasks_key 分辨資料有沒有變），避免每次頁面重跑
    （例如按個按鈕）都重新呼叫一次 Gemini，浪費額度也拖慢速度。
    """
    overdue_items = [t for t in tasks if t.get("status") == "進行中" and (t.get("overdue_days") or 0) > 0]
    overdue_items.sort(key=lambda t: t.get("overdue_days") or 0, reverse=True)
    decide_items = [t for t in tasks if t.get("decide") == "待決議"]

    risk_input = {
        "統計數字": stats,
        "逾期任務_前10筆": [
            {"專案": t.get("proj"), "任務": t.get("task"), "負責人": t.get("owner"), "逾期天數": t.get("overdue_days")}
            for t in overdue_items[:10]
        ],
        "待決議任務": [
            {"專案": t.get("proj"), "任務": t.get("task"), "負責人": t.get("owner")}
            for t in decide_items[:10]
        ],
    }

    sys_prompt = f"""你是 BPM Team 的專案風險分析助理。根據以下資料，用繁體中文寫出 2-4 條「真正值得注意」的風險重點，
每條一行、簡潔有力，前面加上適當的 emoji（例如 ⚠️ 🔴 📌）。只寫真正有風險訊號的重點（例如某專案逾期特別集中、
某人手上逾期任務特別多、待決議事項卡很久），如果資料顯示一切正常（沒有逾期、沒有待決議），就只回一句話說明目前狀況良好，
不要硬湊風險出來。不要輸出任何 JSON，只輸出給人看的重點條列文字。

資料：{json.dumps(risk_input, ensure_ascii=False)}"""

    ok, result = call_gemini_with_retry(sys_prompt, [{"role": "user", "parts": [{"text": "請給我風險摘要"}]}], max_retries=1)
    if not ok:
        return f"（風險摘要暫時無法產生：{result}）"
    return result


def generate_weekly_report(tasks: list, stats: dict, today: date) -> str:
    """產生一份可以直接複製貼到 Outlook / Teams 的週報文字。
    不用快取（使用者按按鈕才會呼叫，本來就是低頻動作，且每次按都該反映最新狀態）。
    """
    week_ago = today - timedelta(days=7)
    week_ahead = today + timedelta(days=7)

    def _in_range(d_str, start, end):
        d = _to_date(d_str)
        return d is not None and start <= d <= end

    done_this_week = [t for t in tasks if t.get("status") == "已完成" and _in_range(t.get("actual_end"), week_ago, today)]
    overdue_now = [t for t in tasks if t.get("status") == "進行中" and (t.get("overdue_days") or 0) > 0]
    overdue_now.sort(key=lambda t: t.get("overdue_days") or 0, reverse=True)
    decide_pending = [t for t in tasks if t.get("decide") == "待決議"]
    due_next_week = [t for t in tasks if t.get("status") != "已完成" and _in_range(t.get("end"), today, week_ahead)]

    def _brief(items, limit=15):
        return [
            {"專案": t.get("proj"), "任務": t.get("task"), "負責人": t.get("owner"), "結束日": t.get("end"), "逾期天數": t.get("overdue_days")}
            for t in items[:limit]
        ]

    report_input = {
        "統計數字": stats,
        "本週完成": _brief(done_this_week),
        "目前逾期(進行中)": _brief(overdue_now),
        "待決議事項": _brief(decide_pending),
        "未來7天到期": _brief(due_next_week),
    }

    sys_prompt = f"""你是 BPM Team 的專案助理，請根據以下資料寫一份「週報」，用繁體中文，格式要能直接複製貼到 Outlook 郵件或 Teams 訊息裡使用（不要用 Markdown 的 # 標題語法，用純文字加上「【】」分段即可）。

結構請包含：
【本週摘要】整體進度一句話總結（引用統計數字，不要自己數）
【本週完成】列出本週完成的任務（專案+任務名稱，簡短）；若沒有就寫「本週沒有任務完成」
【逾期提醒】列出目前逾期中的任務，依逾期天數排序，標出負責人；若沒有就寫「目前沒有逾期任務」
【待決議事項】列出還沒決議的項目跟負責人；若沒有就省略這段
【未來一週注意】列出未來 7 天內到期、還沒完成的任務，提醒負責人留意

語氣專業、簡潔，不要加多餘的客套話或表情符號堆疊（最多在標題前用 1 個 emoji 點綴）。

資料快照日期：{today.isoformat()}
資料：{json.dumps(report_input, ensure_ascii=False)}"""

    ok, result = call_gemini_with_retry(sys_prompt, [{"role": "user", "parts": [{"text": "請幫我產生本週的週報"}]}], max_retries=1)
    if not ok:
        return f"⚠️ 週報產生失敗：{result}"
    return result


with st.spinner("從 Jira 載入資料中..."):
    tasks, errors = fetch_all_tasks()

for e in errors:
    st.warning(f"⚠️ {e}")

if not tasks:
    st.error("無法載入任何任務資料，請確認 Jira Token / 網域是否正確、專案是否存在。")
    st.stop()

today_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

# ── 主動風險摘要：不用開聊天室、不用主動問，就會自動顯示 ──
_stats = compute_stats(tasks)
# 用任務清單的內容做快取鍵，資料真的變了（例如按了更新資料）才會重新呼叫 Gemini，
# 否則使用者點其他按鈕造成的頁面重跑，不會浪費一次 Gemini 呼叫額度。
_tasks_key = json.dumps(_stats, sort_keys=True, ensure_ascii=False)
with st.spinner("正在產生風險摘要..."):
    _risk_summary = generate_risk_summary(_tasks_key, tasks, _stats)
with st.container(border=True):
    st.markdown(f"**🔎 AI 風險摘要**（資料快照 {today_str}）")
    st.markdown(_risk_summary)

# ── 週報產生（按鈕在最上面按鈕列，這裡處理點擊後的產生與顯示）──
if gen_report_clicked:
    with st.spinner("正在產生週報..."):
        st.session_state.weekly_report = generate_weekly_report(tasks, _stats, date.today())

if st.session_state.weekly_report:
    with st.container(border=True):
        st.markdown("**📄 週報（可直接複製貼到 Outlook / Teams）**")
        st.code(st.session_state.weekly_report, language=None)

tasks_json = json.dumps(tasks, ensure_ascii=False)
# 防護：欄位內容若剛好包含 "</script>"，未跳脫會提前關閉整段 <script>，
# 導致頁面壞掉甚至有 XSS 風險，因此把 "</" 轉成 JS 可安全解析的 "<\/"。
tasks_json = tasks_json.replace("</", "<\\/")

HTML_PATH = Path(__file__).parent / "dashboard.html"
if not HTML_PATH.exists():
    st.error(f"找不到 {HTML_PATH.name}，請確認它和 app.py 放在 repo 同一層。")
    st.stop()

html = HTML_PATH.read_text(encoding="utf-8")
html = html.replace("__SNAPSHOT_DATETIME__", today_str)
html = html.replace("__TASKS_JSON__", tasks_json)
html = html.replace("__GEMINI_API_KEY__", GEMINI_API_KEY)

# ── Dashboard 小幫手 ──
if st.session_state.show_chat:
    chat_col, dash_col = st.columns([1, 2])
    with dash_col:
        components.html(html, height=1200, scrolling=False)
    with chat_col:
        st.markdown(
            """
            <style>
            .chat-header {
                background: white; border: 1px solid #e8eaed; box-shadow: 0 2px 8px rgba(0,0,0,0.08);
                border-radius: 12px;
                padding: 16px 20px;
                margin-bottom: 12px;
                display: flex;
                align-items: center;
                gap: 12px;
            }
            .chat-header img { width: 32px; height: 32px; object-fit: contain; }
            .chat-header h3 { color: #202124; margin: 0; font-size: 16px; font-weight: 600; }
            .chat-header p { color: #80868b; margin: 0; font-size: 12px; }
            </style>
            <div class="chat-header">
                <img src="https://raw.githubusercontent.com/HarryYang-ALP/AD-chatbot/main/logo.png" alt="ALP">
                <div>
                    <h3>Dashboard 小幫手</h3>
                    <p>可詢問專案進度或直接更新任務資料</p>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # 確認更新的 UI（同時支援單筆 action:"update" 跟批次 action:"batch_update"）
        if st.session_state.pending_update:
            pu = st.session_state.pending_update
            items = pu.get("items") if pu.get("action") == "batch_update" else [pu]
            item_lines = "\n".join(f"- **{it['task']}**：{it['description']}" for it in items)
            st.warning(f"**確認修改（共 {len(items)} 筆）：**\n\n{item_lines}")
            col_y, col_n = st.columns(2)
            with col_y:
                if st.button("✅ 確認", use_container_width=True):
                    all_errs = []
                    success_lines = []
                    for it in items:
                        errs = update_jira_issue(it["issue_key"], it["updates"])
                        if errs:
                            all_errs.append(f"{it['task']}：{'; '.join(errs)}")
                        else:
                            success_lines.append(f"✅ **{it['task']}**：{it['description']}")
                    if all_errs:
                        st.error("以下項目更新失敗：\n" + "\n".join(all_errs))
                    if success_lines:
                        st.success(f"成功更新 {len(success_lines)} 筆！")
                        st.session_state.ad_msg.append({"role": "assistant", "content": "\n".join(success_lines)})
                    if not all_errs:
                        st.session_state.pending_update = None
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        st.session_state.pending_update = None
            with col_n:
                if st.button("❌ 取消", use_container_width=True):
                    st.session_state.pending_update = None
                    st.session_state.ad_msg.append({"role": "assistant", "content": "已取消更新。"})
                    st.rerun()

        chat_container = st.container(height=400)
        with chat_container:
            for m in st.session_state.ad_msg:
                with st.chat_message(m["role"]):
                    st.markdown(m["content"])

        if prompt := st.chat_input("問我專案進度，或說「把 XX 任務狀態改成進行中」", key="ad_chat"):
            st.session_state.ad_msg.append({"role": "user", "content": prompt})
            st.session_state.ad_hist.append({"role": "user", "parts": [{"text": prompt}]})
            with chat_container:
                with st.chat_message("user"):
                    st.markdown(prompt)

            # 任務清單摘要給 AI
            tasks_summary = json.dumps([{
                "issue_key": t.get("issue_key", ""),
                "專案": t.get("proj", ""),
                "任務": t.get("task", ""),
                "狀態": t.get("status", ""),
                "負責人": t.get("owner", ""),
                "進度": str(t.get("progress", "")) + "%",
                "結束日": t.get("end", ""),
                "逾期天數": t.get("overdue_days", 0),
                "須決議": t.get("decide", ""),
                "優先": t.get("prio", ""),
                "進度說明": t.get("prog_note", ""),
            } for t in tasks], ensure_ascii=False)

            # 統計類數字用共用的 compute_stats() 算，跟自動風險摘要、Dashboard KPI 是同一套邏輯，
            # 不會有「聊天室講的數字」跟「畫面上顯示的數字」對不上的情況。
            stats_summary = json.dumps(compute_stats(tasks), ensure_ascii=False)

            _sys = f"""你是 BPM Team 的專案進度助理，可以回答問題也可以協助更新 Jira 任務資料。
資料快照：{today_str}

【已經算好的統計數字（極重要）】
{stats_summary}
如果使用者問的是「總共/已完成/進行中/未開始/逾期/須決議 有幾件」這類統計性問題，
或是問某個專案有幾件、已完成幾件，一律直接引用上面這包已經算好的數字回答，
絕對不要自己重新從下面的任務清單一筆一筆數，你數的結果不可靠，這包數字才是正確答案。

任務資料（明細，用於查詢特定任務、負責人、日期等非統計性問題）：{tasks_summary}

【回答規則】
1. 若使用者在問問題，用繁體中文簡潔回答。回答時不要顯示 Jira issue key（如 BPM-8、AHP-4 等），只用專案名稱和任務名稱表示。
2. 若使用者要修改「單一」任務資料，請回傳以下 JSON 格式（只回傳 JSON，不要其他文字）：
{{
  "action": "update",
  "issue_key": "BPM-X",
  "task": "任務名稱",
  "description": "把XX改成YY",
  "updates": {{
    "狀態": "已完成"
  }}
}}
3. 若使用者要求「批次」修改多筆任務（例如「把所有逾期的進行中任務都標記須決議」「把 XX 專案還沒開始的都改成進行中」），
   請先自己從上面的任務清單裡篩選出符合條件的任務，再回傳以下批次格式（同樣只回傳 JSON，不要其他文字）：
{{
  "action": "batch_update",
  "description": "整體修改說明，例如：把 3 筆逾期任務標記為須決議",
  "items": [
    {{"issue_key": "BPM-X", "task": "任務名稱1", "description": "把狀態改成已完成", "updates": {{"狀態": "已完成"}}}},
    {{"issue_key": "AHP-Y", "task": "任務名稱2", "description": "把須決議改成待決議", "updates": {{"須決議": "待決議"}}}}
  ]
}}
   批次一次最多處理 20 筆，超過的話請告訴使用者篩選結果太多，請他縮小條件。
4. 嚴格規則：
   - 單筆修改（action: update）只能修改使用者明確指定的那一個任務。
   - 批次修改（action: batch_update）裡的每一筆，都必須是根據使用者說的條件、從上面任務清單篩選出來的真實任務，不能捏造不存在的 issue_key。
   - "進度" 欄位由系統根據狀態自動計算（已完成=100%，未開始=0%，進行中=50%），不需要也不能單獨修改進度。
   - 可修改的欄位只有：狀態（未開始/進行中/已完成）、負責人、結束日（YYYY-MM-DD）、進度說明、優先、須決議。
5. 若找不到符合條件的任務請說明，不要硬湊。"""

            _reply = "抱歉，發生錯誤。"
            with chat_container:
                with st.chat_message("assistant"):
                    with st.spinner("處理中..."):
                        ok, result = call_gemini_with_retry(_sys, st.session_state.ad_hist)

                        if not ok:
                            # 明確顯示是哪種問題（逾時／流量上限／金鑰錯誤／安全性過濾...），
                            # 不再讓使用者看到一串看不懂的原始例外文字。
                            _reply = f"⚠️ {result}"
                            st.markdown(_reply)
                        else:
                            _reply = result
                            # 嘗試解析是否為更新指令（單筆 update 或批次 batch_update）
                            try:
                                _clean = _reply.strip().strip("```json").strip("```").strip()
                                _cmd = json.loads(_clean)
                                if _cmd.get("action") == "update":
                                    st.session_state.pending_update = _cmd
                                    _reply = f"我準備幫你修改 **{_cmd['task']}**：{_cmd['description']}\n\n請確認是否執行？"
                                elif _cmd.get("action") == "batch_update":
                                    _items = _cmd.get("items", [])
                                    st.session_state.pending_update = _cmd
                                    _preview = "\n".join(f"- {it['task']}：{it['description']}" for it in _items[:10])
                                    _more = f"\n...等共 {len(_items)} 筆" if len(_items) > 10 else ""
                                    _reply = f"我準備批次修改 {len(_items)} 筆任務（{_cmd.get('description','')}）：\n\n{_preview}{_more}\n\n請確認是否執行？"
                            except Exception:
                                pass  # 不是 JSON，當一般回答處理

                            st.markdown(_reply)

            st.session_state.ad_msg.append({"role": "assistant", "content": _reply})
            st.session_state.ad_hist.append({"role": "model", "parts": [{"text": _reply}]})
            # 對話歷史每次都會整包送給 Gemini，放著不管會越滾越大（越用越慢，甚至可能
            # 超過長度限制報錯）。只保留最近 20 則（約 10 輪對話），避免無限累積。
            MAX_HIST = 20
            if len(st.session_state.ad_hist) > MAX_HIST:
                st.session_state.ad_hist = st.session_state.ad_hist[-MAX_HIST:]
            st.rerun()
else:
    components.html(html, height=1200, scrolling=False)
