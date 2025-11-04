import os
import uuid
import sqlite3
import pandas as pd
import streamlit as st
import plotly.express as px

DB_PATH = "vote.db"

# ========================
# 🔧 資料庫連線/啟動
# ========================
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()

# --- 建表 ---
cur.execute("""
CREATE TABLE IF NOT EXISTS community (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS voter (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    email TEXT,
    community_id INTEGER,
    token TEXT UNIQUE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (community_id) REFERENCES community(id)
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS question (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    description TEXT,
    community_id INTEGER,
    FOREIGN KEY (community_id) REFERENCES community(id)
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS vote (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    voter_id INTEGER,
    question_id INTEGER,
    choice TEXT CHECK(choice IN ('同意', '不同意', '沒意見')),
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (voter_id) REFERENCES voter(id),
    FOREIGN KEY (question_id) REFERENCES question(id)
)
""")

# --- 關鍵唯一索引（避免重覆 & 讓 UPSERT 生效）---
cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_voter_email_comm ON voter(email, community_id)")
cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_question_comm_title ON question(community_id, title)")
cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_vote_voter_question ON vote(voter_id, question_id)")

# --- 系統設定（布林旗標，避免日期設定）---
cur.execute("""
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK(id=1),
    voting_open INTEGER DEFAULT 1,   -- 1 開啟投票（可改票）、0 關閉
    results_open INTEGER DEFAULT 0   -- 1 開放看結果、0 關閉
)
""")
cur.execute("INSERT OR IGNORE INTO settings (id, voting_open, results_open) VALUES (1, 1, 0)")
conn.commit()

# ========================
# 🧭 路由（以 URL Query Params 控制三頁）
# ========================
params = st.query_params
page = params.get("page", "vote")  # 預設進投票頁

def nav_links():
    st.markdown(
        """
        <div style="display:flex; gap:12px; margin:8px 0 16px 0;">
          <a href="?page=vote">使用者投票頁</a>
          <a href="?page=results">結果頁</a>
          <a href="?page=admin">管理者頁</a>
        </div>
        """,
        unsafe_allow_html=True
    )

# ========================
# 🔐 管理者驗證（環境變數 APP_ADMIN_KEY）
# ========================
ADMIN_KEY = os.environ.get("APP_ADMIN_KEY", "")  # 部署時務必設定
def admin_logged_in() -> bool:
    return st.session_state.get("is_admin", False)

def admin_login_ui():
    st.subheader("管理者登入")
    key = st.text_input("請輸入管理者金鑰", type="password")
    if st.button("登入管理者"):
        if ADMIN_KEY and key == ADMIN_KEY:
            st.session_state["is_admin"] = True
            st.success("已登入管理者")
            st.rerun()
        else:
            st.error("金鑰錯誤或尚未設定 APP_ADMIN_KEY")

# ========================
# 🧰 共用工具
# ========================
def upsert_community(name: str) -> int:
    cur.execute("INSERT OR IGNORE INTO community(name) VALUES (?)", (name,))
    cur.execute("SELECT id FROM community WHERE name=?", (name,))
    return cur.fetchone()[0]

def process_voters_df(df: pd.DataFrame, regenerate_tokens: bool):
    # 欄位檢查
    required = {"name", "email", "community"}
    if not required.issubset(df.columns):
        raise ValueError(f"voters.xlsx 欄位需包含：{required}")

    for _, row in df.iterrows():
        comm_id = upsert_community(str(row["community"]).strip())
        name = str(row["name"]).strip()
        email = str(row["email"]).strip()

        # 先查既有 voter
        cur.execute(
            "SELECT id, token FROM voter WHERE email=? AND community_id=?",
            (email, comm_id),
        )
        found = cur.fetchone()

        token = uuid.uuid4().hex[:8] if (regenerate_tokens or not (found and found[1])) else found[1]

        # 以 (email, community_id) 為唯一鍵 UPSERT，保留/更新姓名與 token 規則
        cur.execute("""
            INSERT INTO voter(name, email, community_id, token)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(email, community_id)
            DO UPDATE SET
                name=excluded.name,
                token=CASE WHEN ?=1 THEN excluded.token ELSE voter.token END
        """, (name, email, comm_id, token, 1 if regenerate_tokens else 0))

    conn.commit()

def export_login_list():
    df_out = pd.read_sql_query("""
        SELECT v.name AS voter_name, v.email, c.name AS community, v.token
        FROM voter v
        JOIN community c ON v.community_id = c.id
        ORDER BY c.name, v.name
    """, conn)
    df_out.to_excel("登入名單.xlsx", index=False)
    with open("登入名單.xlsx", "rb") as f:
        st.download_button("📥 下載登入名單.xlsx", f, "登入名單.xlsx")

def process_questions_df(df: pd.DataFrame):
    required = {"community", "title", "description"}
    if not required.issubset(df.columns):
        raise ValueError(f"questions.xlsx 欄位需包含：{required}")

    for _, row in df.iterrows():
        comm_id = upsert_community(str(row["community"]).strip())
        title = str(row["title"]).strip()
        desc = str(row["description"]).strip()

        # 以 (community_id, title) 為唯一鍵 UPSERT（避免重覆新增）
        cur.execute("""
            INSERT INTO question(title, description, community_id)
            VALUES (?, ?, ?)
            ON CONFLICT(community_id, title)
            DO UPDATE SET description=excluded.description
        """, (title, desc, comm_id))
    conn.commit()

def get_settings():
    r = pd.read_sql_query("SELECT voting_open, results_open FROM settings WHERE id=1", conn).iloc[0]
    return bool(r["voting_open"]), bool(r["results_open"])

def set_settings(voting_open: bool = None, results_open: bool = None):
    if voting_open is not None:
        cur.execute("UPDATE settings SET voting_open=? WHERE id=1", (1 if voting_open else 0,))
    if results_open is not None:
        cur.execute("UPDATE settings SET results_open=? WHERE id=1", (1 if results_open else 0,))
    conn.commit()

def require_token_login():
    if "token" not in st.session_state:
        token_input = st.text_input("請輸入您的投票代碼 (token)")
        if st.button("登入"):
            cur.execute("SELECT id, name, community_id FROM voter WHERE token=?", (token_input,))
            user = cur.fetchone()
            if user:
                st.session_state["token"] = token_input
                st.session_state["user_id"] = user[0]
                st.session_state["user_name"] = user[1]
                st.session_state["community_id"] = user[2]
                st.rerun()
            else:
                st.error("無效的代碼")
        st.stop()  # 未登入就停止渲染後續內容

def vote_upsert(voter_id: int, question_id: int, choice: str):
    # 以 (voter_id, question_id) 唯一鍵 UPSERT，允許改票
    cur.execute("""
        INSERT INTO vote(voter_id, question_id, choice)
        VALUES (?, ?, ?)
        ON CONFLICT(voter_id, question_id)
        DO UPDATE SET choice=excluded.choice, timestamp=CURRENT_TIMESTAMP
    """, (voter_id, question_id, choice))
    conn.commit()

# ========================
# 🗳️ 頁面：使用者投票
# ========================
# 放在工具區域：取得使用者既有投票（question_id -> choice）
def get_existing_votes(voter_id: int, community_id: int) -> dict:
    df = pd.read_sql_query("""
        SELECT q.id AS question_id, v.choice
        FROM question q
        LEFT JOIN vote v
          ON v.question_id = q.id AND v.voter_id = ?
        WHERE q.community_id = ?
        ORDER BY q.id ASC
    """, conn, params=(voter_id, community_id))
    return {int(r["question_id"]): (None if pd.isna(r["choice"]) else str(r["choice"])) for _, r in df.iterrows()}

# 取代原本的 page_vote()
def page_vote():
    st.title("🗳️ 社區投票")
    # nav_links()
    voting_open, _ = get_settings()

    require_token_login()  # token 驗證（未登入會 st.stop()）

    # 取題目
    cur.execute(
        "SELECT id, title, description FROM question WHERE community_id=? ORDER BY id ASC",
        (st.session_state["community_id"],)
    )
    questions = cur.fetchall()

    if not questions:
        st.info("尚無可投票題目。")
        return

    # 讀取既有投票 → 初始化到 session_state keys：choice_{qid}
    existing = get_existing_votes(st.session_state["user_id"], st.session_state["community_id"])
    for qid, prev_choice in existing.items():
        key = f"choice_{qid}"
        if key not in st.session_state:
            st.session_state[key] = prev_choice  # 讓 radio 預設值等於上次選擇（或 None）

    st.markdown("> 提醒：投票開啟期間可重複更新答案；關閉後將無法變更。")

    # 顯示所有題目（同頁）
    OPTIONS = ["同意", "不同意", "沒意見"]
    for qid, title, desc in questions:
        with st.container(border=True):
            st.markdown(f"**題目 #{qid}：{title}**")
            if desc:
                st.caption(desc)

            # 用固定 key 維持狀態，不會因 rerun 把內容收回
            st.radio(
                "您的選擇：",
                OPTIONS,
                key=f"choice_{qid}",
                index=(OPTIONS.index(st.session_state[f'choice_{qid}']) 
                       if st.session_state[f'choice_{qid}'] in OPTIONS else None),
                disabled=not voting_open,
                horizontal=True,
            )

    # 操作列
    col1, col2 = st.columns([1,1])
    with col1:
        disabled_msg = "（目前投票已關閉）" if not voting_open else ""
        submit = st.button(f"✅ 送出 / 更新全部投票{disabled_msg}", disabled=not voting_open)
    with col2:
        if st.button("🚪 登出"):
            for k in ["token","user_id","user_name","community_id","current_choice","temp_choice"]:
                st.session_state.pop(k, None)
            st.rerun()

    # 一次寫入所有有選擇的題目
    if submit:
        updated = 0
        for qid, _, _ in questions:
            choice = st.session_state.get(f"choice_{qid}")
            if choice in OPTIONS:
                vote_upsert(st.session_state["user_id"], int(qid), choice)
                updated += 1

        if updated == 0:
            st.warning("尚未選擇任何題目。請至少選擇一題再送出。")
        else:
            st.success(f"已更新 {updated} 題投票結果。")
            st.rerun()


# ========================
# 📊 頁面：結果（需結果開放）
# ========================
def page_results():
    st.title("📊 投票結果")
    # nav_links()
    _, results_open = get_settings()

    require_token_login()

    if not results_open:
        st.info("尚未開放結果查看，請稍後再試。")
        return

    # 只看自己社區
    comm_id = st.session_state["community_id"]
    cur.execute("SELECT id, title FROM question WHERE community_id=?", (comm_id,))
    questions = cur.fetchall()
    if not questions:
        st.info("尚無題目。")
        return

    selected_title = st.selectbox("選擇題目（顯示本社區統計）", [q[1] for q in questions])
    qid = next(q[0] for q in questions if q[1] == selected_title)

    # 統計
    df = pd.read_sql_query("""
        SELECT v.choice, COUNT(*) AS cnt
        FROM vote v
        WHERE v.question_id=?
        GROUP BY v.choice
    """, conn, params=(qid,))

    if df.empty:
        st.info("此題尚無投票。")
    else:
        fig = px.bar(df, x="choice", y="cnt", text="cnt", title="目前投票結果（本社區）")
        st.plotly_chart(fig, use_container_width=True)

    # 個人投票紀錄（自己）
    me = pd.read_sql_query("""
        SELECT q.title, v.choice, v.timestamp
        FROM vote v
        JOIN question q ON v.question_id = q.id
        WHERE v.voter_id=? AND q.community_id=?
        ORDER BY v.timestamp DESC
    """, conn, params=(st.session_state["user_id"], comm_id))
    st.markdown("#### 我的投票紀錄")
    st.dataframe(me, use_container_width=True)

    # 登出
    if st.button("🚪 登出"):
        for k in ["token","user_id","user_name","community_id","current_choice","temp_choice"]:
            st.session_state.pop(k, None)
        st.rerun()

# ========================
# 🛠️ 頁面：管理者
# ========================
def page_admin():
    st.title("🛠️ 投票管理")
    # nav_links()

    if not admin_logged_in():
        admin_login_ui()
        return

    # 狀態開關
    voting_open, results_open = get_settings()
    col1, col2 = st.columns(2)
    with col1:
        new_voting_open = st.toggle("投票開啟（允許改票）", value=voting_open, help="關閉後使用者不可修改/送出投票")
    with col2:
        new_results_open = st.toggle("結果開放", value=results_open, help="開放後使用者可在結果頁看到本社區統計")

    if st.button("💾 儲存設定"):
        set_settings(new_voting_open, new_results_open)
        st.success("設定已更新")
        st.rerun()

    st.divider()
    st.subheader("📤 上傳 Excel（覆蓋更新，不提供 CRUD）")

    voters_file = st.file_uploader("上傳人員名單 voters.xlsx（欄位：name, email, community）", type="xlsx", key="voters_up")
    regen = st.checkbox("重新產生所有上傳名單的 token（若不勾選：已有 token 則沿用）", value=False)
    if st.button("📥 匯入人員名單"):
        if not voters_file:
            st.warning("請先選擇 voters.xlsx")
        else:
            try:
                df = pd.read_excel(voters_file)
                process_voters_df(df, regenerate_tokens=regen)
                st.success("人員名單已更新")
                export_login_list()
            except Exception as e:
                st.error(f"匯入失敗：{e}")

    questions_file = st.file_uploader("上傳題目名單 questions.xlsx（欄位：community, title, description）", type="xlsx", key="questions_up")
    if st.button("📥 匯入題目名單"):
        if not questions_file:
            st.warning("請先選擇 questions.xlsx")
        else:
            try:
                df = pd.read_excel(questions_file)
                process_questions_df(df)
                st.success("題目名單已更新（相同社區+標題會覆寫 description）")
            except Exception as e:
                st.error(f"匯入失敗：{e}")

    st.divider()
    st.subheader("🧾 檢視統計（快速概覽）")
    stats = {}
    stats["社區數"] = pd.read_sql_query("SELECT COUNT(*) AS n FROM community", conn)["n"][0]
    stats["投票人數"] = pd.read_sql_query("SELECT COUNT(*) AS n FROM voter", conn)["n"][0]
    stats["題目數"] = pd.read_sql_query("SELECT COUNT(*) AS n FROM question", conn)["n"][0]
    stats["投票紀錄數"] = pd.read_sql_query("SELECT COUNT(*) AS n FROM vote", conn)["n"][0]
    st.write(stats)

    st.markdown("#### 下載目前登入名單（含 token）")
    export_login_list()

    if st.button("🚪 登出管理者"):
        st.session_state["is_admin"] = False
        st.rerun()

# ========================
# 🏁 進入點
# ========================
if page == "admin":
    page_admin()
elif page == "results":
    page_results()
else:
    page_vote()
