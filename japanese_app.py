import streamlit as st
import pandas as pd
import random
import asyncio
import edge_tts
from io import BytesIO
import speech_recognition as sr
from streamlit_mic_recorder import mic_recorder
import pykakasi 
from datetime import datetime, timedelta
import re
import difflib
from streamlit_gsheets import GSheetsConnection

# --- 設定區 ---
# 不再需要 DATA_FILENAME, MISTAKE_FILENAME 等，全部由 Google Sheets 管理
TEMP_AUDIO_FILE = "temp_jp_voice.mp3"

# --- 1. Google Sheets 核心連線與讀寫 ---

def get_db_connection():
    return st.connection("gsheets", type=GSheetsConnection)

def load_data_from_sheet():
    conn = get_db_connection()
    try:
        # read(ttl=0) 確保每次都讀取最新資料，不快取
        df = conn.read(ttl=0)
        
        # 補齊必要欄位，防止新 Sheet 缺少欄位報錯
        expected_cols = ["Sentence", "Translation", "Group", "Parsing", 
                         "Vocab List", "Meaning", "Time", "Weak", 
                         "Next_Review", "Interval", "Reps"]
        
        for col in expected_cols:
            if col not in df.columns:
                df[col] = None
        
        # 資料清理
        df = df.fillna("")
        return df
    except Exception as e:
        st.error(f"Google Sheets 連線失敗: {e}")
        return pd.DataFrame()

def save_data_to_sheet(df):
    conn = get_db_connection()
    try:
        conn.update(data=df)
    except Exception as e:
        st.error(f"寫入 Google Sheets 失敗: {e}")

# --- 2. 資料解析 (DataFrame -> App 格式) ---

def parse_data(df):
    sentence_data = [] 
    vocab_data = []    
    group_map = {}     
    
    all_sentence_translations = []
    all_vocab_meanings = []
    
    srs_map = {} # 用來快速查找 SRS 狀態
    mistakes_list = [] # 用來快速查找錯題

    default_date = datetime.now().strftime("%Y-%m-%d")

    for idx, row in df.iterrows():
        # --- 通用欄位處理 ---
        time_str = str(row.get('Time', default_date)).strip()
        if not time_str: time_str = default_date
        try:
             # 嘗試正規化日期
             time_str = pd.to_datetime(time_str).strftime("%Y-%m-%d")
        except: pass

        # --- SRS 數據讀取 ---
        next_review = str(row.get('Next_Review', '')).strip()
        if not next_review: next_review = default_date # 預設今天
        try:
            next_review = pd.to_datetime(next_review).strftime("%Y-%m-%d")
        except: next_review = default_date

        try:
            interval = int(float(row.get('Interval', 0) or 0))
            reps = int(float(row.get('Reps', 0) or 0))
        except:
            interval = 0
            reps = 0

        is_weak = str(row.get('Weak', '')).strip().lower() in ['yes', 'true', '1']

        # --- 句子解析 ---
        s_ja = str(row.get('Sentence', '')).strip()
        s_ch = str(row.get('Translation', '')).strip()
        gid  = str(row.get('Group', '')).strip()
        
        # Parsing 處理
        parsing_raw = str(row.get('Parsing', '')).strip().replace('＋', '+')
        
        if s_ja:
            # 建立句子資料
            item = {
                "type": "sentence",
                "sentence": s_ja,
                "translation": s_ch,
                "group": gid,
                "parsing": [p.strip() for p in parsing_raw.split('+') if p.strip()],
                "start_date": time_str,
                "row_idx": idx # 記住 Row Index 以便更新
            }
            sentence_data.append(item)
            all_sentence_translations.append(s_ch)
            
            if gid:
                if gid not in group_map: group_map[gid] = []
                if s_ja not in group_map[gid]: group_map[gid].append(s_ja)
            
            # 存入 SRS Map (Key: Sentence)
            srs_map[s_ja] = {"next_review": next_review, "interval": interval, "reps": reps, "row_idx": idx}
            if is_weak: mistakes_list.append(s_ja)

        # --- 單字解析 ---
        v_list_raw = str(row.get('Vocab List', '')).strip()
        m_list_raw = str(row.get('Meaning', '')).strip()
        
        if v_list_raw and m_list_raw:
            v_items = [x.strip() for x in v_list_raw.split('。') if x.strip()]
            m_items = [x.strip() for x in m_list_raw.split('。') if x.strip()]
            
            if len(v_items) == len(m_items):
                for i, v_str in enumerate(v_items):
                    if '｜' in v_str:
                        kanji, reading = v_str.split('｜', 1)
                    else:
                        kanji, reading = v_str, v_str
                    
                    kanji = kanji.strip()
                    
                    # ⚠️ 注意：單字目前共用同一行的 SRS 數據
                    # 若要精確追蹤每個單字，Sheet 結構需改變。目前簡化為：單字題更新整行數據。
                    v_item = {
                        "type": "vocab",
                        "kanji": kanji,
                        "reading": reading.strip(),
                        "meaning": m_items[i],
                        "start_date": time_str,
                        "row_idx": idx 
                    }
                    vocab_data.append(v_item)
                    all_vocab_meanings.append(m_items[i])
                    
                    # 存入 SRS Map (Key: Kanji)
                    # 注意：如果同一行有多個單字，Key 不同但 Row Index 相同
                    srs_map[kanji] = {"next_review": next_review, "interval": interval, "reps": reps, "row_idx": idx}
                    if is_weak: mistakes_list.append(kanji)

    return sentence_data, vocab_data, group_map, (all_sentence_translations, all_vocab_meanings), srs_map, mistakes_list

# --- 3. SRS 更新邏輯 (寫回 DataFrame 並上傳) ---

def update_srs_status_sheet(key, is_correct, row_idx):
    df = st.session_state.raw_df # 取得目前的 DataFrame
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    # 讀取當前數值
    try:
        current_interval = int(float(df.at[row_idx, "Interval"] or 0))
        current_reps = int(float(df.at[row_idx, "Reps"] or 0))
    except:
        current_interval = 0
        current_reps = 0
    
    if is_correct:
        # 答對：拉長間隔
        if current_interval == 0: new_interval = 1
        elif current_interval == 1: new_interval = 3
        else: new_interval = int(current_interval * 2.2)
        new_reps = current_reps + 1
        is_weak = "No" # 答對移除 Weak 標記
    else:
        # 答錯：重置
        new_interval = 0
        new_reps = 0
        is_weak = "Yes" # 標記為 Weak
        
    next_date = datetime.now() + timedelta(days=new_interval)
    new_next_review = next_date.strftime("%Y-%m-%d")
    
    # 更新 DataFrame
    df.at[row_idx, "Interval"] = new_interval
    df.at[row_idx, "Reps"] = new_reps
    df.at[row_idx, "Next_Review"] = new_next_review
    df.at[row_idx, "Weak"] = is_weak
    
    # 寫回 Google Sheets
    # 為了效能，這裡每次答題都寫入。若覺得慢，可改為只更新 session_state df，另設一顆按鈕「儲存進度」
    save_data_to_sheet(df)
    
    # 更新 Session State 中的暫存，以免頁面沒重整讀到舊資料
    st.session_state.raw_df = df
    # 同步更新 srs_map
    if key in st.session_state.srs_map:
        st.session_state.srs_map[key] = {
            "next_review": new_next_review,
            "interval": new_interval,
            "reps": new_reps,
            "row_idx": row_idx
        }
    
    # 同步更新 mistakes_list
    if is_correct:
        if key in st.session_state.mistakes_list:
            st.session_state.mistakes_list.remove(key)
    else:
        if key not in st.session_state.mistakes_list:
            st.session_state.mistakes_list.append(key)
            
    return new_interval, new_next_review

# --- 4. 輔助工具 (TTS, Diff, Kakasi) ---

async def _edge_tts_save(text, voice="ja-JP-KeitaNeural"):
    try:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(TEMP_AUDIO_FILE)
        return True
    except Exception as e: return False

def get_audio_bytes(text):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success = loop.run_until_complete(_edge_tts_save(text))
        if success:
            with open(TEMP_AUDIO_FILE, "rb") as f: return f.read()
    except: pass
    return None

def get_hiragana(text):
    kks = pykakasi.kakasi()
    result = kks.convert(text)
    return "".join([item['hira'] for item in result])

def generate_diff(user_text, target_text):
    s = difflib.SequenceMatcher(None, user_text, target_text)
    html = []
    for opcode, a0, a1, b0, b1 in s.get_opcodes():
        if opcode == 'equal': html.append(f"<span style='color:green; font-weight:bold'>{target_text[b0:b1]}</span>")
        elif opcode == 'insert': html.append(f"<span style='color:red; text-decoration:underline; background-color:#ffe6e6'>[{target_text[b0:b1]}]</span>")
        elif opcode == 'delete': html.append(f"<span style='color:gray; text-decoration:line-through'>{user_text[a0:a1]}</span>")
        elif opcode == 'replace':
            html.append(f"<span style='color:gray; text-decoration:line-through'>{user_text[a0:a1]}</span>")
            html.append(f"<span style='color:red; background-color:#ffe6e6'>[{target_text[b0:b1]}]</span>")
    return "".join(html)

def transcribe_audio_bytes(audio_bytes):
    r = sr.Recognizer()
    try:
        with sr.AudioFile(BytesIO(audio_bytes)) as source:
            audio_data = r.record(source)
            return r.recognize_google(audio_data, language='ja-JP')
    except: return "Not Recognized"

# --- 5. 初始化與狀態管理 ---

if 'initialized' not in st.session_state:
    with st.spinner("正在連線至 Google Sheets..."):
        df = load_data_from_sheet()
        st.session_state.raw_df = df # 保留原始 DF 以便寫回
        
        s_data, v_data, g_map, pools, srs_map, m_list = parse_data(df)
        
        st.session_state.sentence_data = s_data
        st.session_state.vocab_data = v_data
        st.session_state.group_map = g_map
        st.session_state.trans_pool = pools[0]
        st.session_state.meaning_pool = pools[1]
        
        st.session_state.srs_map = srs_map
        st.session_state.mistakes_list = m_list
        
        st.session_state.current_q = None
        st.session_state.mode = None
        st.session_state.feedback = None
        st.session_state.audio_data = None
        st.session_state.user_audio_bytes = None
        st.session_state.options = []
        st.session_state.shuffled_parsing = []
        st.session_state.selected_indices = []
        
        st.session_state.initialized = True

# --- 6. 核心選題邏輯 ---

def pick_new_question():
    st.session_state.selected_indices = [] 
    st.session_state.shuffled_parsing = []
    st.session_state.feedback = None
    st.session_state.user_audio_bytes = None

    today_str = datetime.now().strftime("%Y-%m-%d")
    srs_map = st.session_state.srs_map
    mistakes = st.session_state.mistakes_list

    # 分類
    due_items = []
    new_items = []

    # 檢查句子
    for item in st.session_state.sentence_data:
        key = item['sentence']
        if key in srs_map:
            if srs_map[key]['next_review'] <= today_str:
                due_items.append(item)
        elif item['start_date'] <= today_str:
            new_items.append(item)

    # 檢查單字
    for item in st.session_state.vocab_data:
        key = item['kanji']
        if key in srs_map:
            if srs_map[key]['next_review'] <= today_str:
                due_items.append(item)
        elif item['start_date'] <= today_str:
            new_items.append(item)

    # 優先級
    q_item = None
    priority_msg = ""

    if due_items:
        q_item = random.choice(due_items)
        priority_msg = "🔥 今日到期 (SRS)"
    elif mistakes and random.random() < 0.7:
        target_key = random.choice(mistakes)
        q_item = next((i for i in st.session_state.sentence_data if i['sentence'] == target_key), None)
        if not q_item:
            q_item = next((i for i in st.session_state.vocab_data if i['kanji'] == target_key), None)
        priority_msg = "💀 錯題複習 (Weak)"
    elif new_items:
        q_item = random.choice(new_items)
        priority_msg = "✨ 新題目"
    else:
        all_pool = st.session_state.sentence_data + st.session_state.vocab_data
        if all_pool:
            q_item = random.choice(all_pool)
            priority_msg = "🎲 隨機練習"
        else:
            st.error("Google Sheets 沒有有效資料！")
            return

    st.session_state.priority_msg = priority_msg
    
    # 決定模式
    if q_item['type'] == 'sentence':
        available_modes = [1, 2, 3, 5, 6, 9]
        if q_item['group'] in st.session_state.group_map and len(st.session_state.group_map[q_item['group']]) >= 2:
            available_modes.append(4)
        mode = random.choice(available_modes)
    else:
        mode = random.choice([7, 8, 10])

    setup_question(q_item, mode)

def setup_question(q_item, mode):
    st.session_state.current_q = q_item
    st.session_state.mode = mode
    
    is_vocab_mode = mode in [7, 8, 10]
    
    # Audio
    if mode in [3, 5, 8, 9, 10]:
        text_to_speak = q_item['kanji'] if is_vocab_mode else q_item['sentence']
        st.session_state.audio_data = get_audio_bytes(text_to_speak)
        
    # Options Generation (略為簡化，與原邏輯相同)
    if mode in [1, 2, 3, 4, 8]:
        pool = []
        correct = ""
        if mode in [1, 3]: 
            correct = q_item['translation']
            pool = st.session_state.trans_pool
        elif mode == 2:
            correct = q_item['sentence']
            pool = [i['sentence'] for i in st.session_state.sentence_data]
        elif mode == 8:
            correct = q_item['meaning']
            pool = st.session_state.meaning_pool
        elif mode == 4:
            # Group Logic
            gid = q_item['group']
            correct = random.choice([s for s in st.session_state.group_map[gid] if s != q_item['sentence']])
            other_gids = [g for g in st.session_state.group_map if g != gid]
            pool = []
            for og in other_gids: pool.extend(st.session_state.group_map[og])
        
        # Safe sample
        distractors = random.sample([x for x in pool if x != correct], min(3, len(pool)))
        final_opts = distractors + [correct]
        random.shuffle(final_opts)
        st.session_state.options = final_opts

    # Parsing setup
    if mode == 6:
        raw_parts = q_item['parsing'].copy() if q_item['parsing'] else [q_item['sentence']]
        indexed_parts = [{'id': i, 'text': t} for i, t in enumerate(raw_parts)]
        random.shuffle(indexed_parts)
        st.session_state.shuffled_parsing = indexed_parts

# --- 7. 作答檢查與回寫 ---

def check_answer(user_input):
    if st.session_state.feedback is not None: return
    item = st.session_state.current_q
    mode = st.session_state.mode
    
    # 標準化答案
    user_clean = str(user_input).replace(" ", "").replace("　", "")
    target = item['translation'] if mode in [1,3] else (item['meaning'] if mode == 8 else (item['reading'] if mode == 7 else item['sentence'] if item['type']=='sentence' else item['kanji']))
    
    # 比對
    is_correct = False
    if mode == 4: is_correct = (user_input in st.session_state.group_map.get(item['group'], []))
    elif mode in [1, 2, 3, 8]: is_correct = (user_clean == str(target).replace(" ", ""))
    else:
        def clean_chars(t): return re.sub(r'[。、？！\?!\s　]', '', str(t))
        is_correct = (get_hiragana(clean_chars(user_input)) == get_hiragana(clean_chars(target)))

    # === 更新 Google Sheets ===
    key = item['sentence'] if item['type'] == 'sentence' else item['kanji']
    row_idx = item['row_idx']
    
    # 呼叫更新函式
    new_interval, next_review_date = update_srs_status_sheet(key, is_correct, row_idx)

    # 產生回饋訊息
    msg_type = "success" if is_correct else "error"
    msg_header = "🎉 正解！" if is_correct else f"❌ 残念... 正解: {target}"
    
    detail_html = f"""
    <br>📅 下次複習: {next_review_date} (間隔: {new_interval} 天)
    <br>💾 已同步至 Google Sheets
    """
    
    st.session_state.feedback = {"type": msg_type, "msg": msg_header + detail_html}
    
    # 若答錯顯示詳細比較
    if not is_correct and mode not in [1,2,3,4,8]:
        st.session_state.feedback["msg"] += f"<br>差異: {generate_diff(str(user_input), str(target))}"

    # 播放正確語音
    speak_text = item['kanji'] if (mode in [7,8,10]) else item['sentence']
    st.session_state.audio_data = get_audio_bytes(speak_text)

# --- Mode 6 輔助 ---
def select_block(idx): st.session_state.selected_indices.append(idx)
def deselect_block(idx): st.session_state.selected_indices.remove(idx)
def submit_parsing():
    lookup = {item['id']: item['text'] for item in st.session_state.shuffled_parsing}
    user_sentence = "".join([lookup[i] for i in st.session_state.selected_indices])
    check_answer(user_sentence)

# --- 8. UI 顯示 ---

st.set_page_config(page_title="雲端日語特訓", page_icon="🇯🇵")

with st.sidebar:
    st.title("☁️ 雲端同步中")
    srs_map = st.session_state.get('srs_map', {})
    mistakes = st.session_state.get('mistakes_list', [])
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    due_count = sum(1 for v in srs_map.values() if v['next_review'] <= today_str)
    st.metric("🔥 今日到期", f"{due_count} 題")
    st.metric("💀 錯題本 (Weak)", f"{len(mistakes)} 題")
    
    if st.button("🔄 強制重整資料"):
        st.cache_data.clear()
        del st.session_state.initialized
        st.rerun()

st.title("🇯🇵 日本語智慧特訓 (G-Sheets Ver.)")

if not st.session_state.get('initialized'):
    st.stop()

if st.session_state.current_q is None:
    pick_new_question()

q = st.session_state.current_q
mode = st.session_state.mode

st.info(f"{st.session_state.get('priority_msg')} | Mode {mode}")

# 顯示題目區 (依照模式)
col1, col2 = st.columns([1, 4])
with col2:
    if mode == 1: st.markdown(f"### {q['sentence']}")
    elif mode == 2: st.markdown(f"### {q['translation']}")
    elif mode == 3: 
        st.write("請聽音檔：")
        if st.session_state.audio_data: st.audio(st.session_state.audio_data, format='audio/mpeg')
    elif mode == 4: 
        st.subheader(f"題目: {q['sentence']}")
        st.write("👉 請選出意思最相近（同群組）的句子")
    elif mode == 5: 
        st.write("請聽音檔並寫下來：")
        if st.session_state.audio_data: st.audio(st.session_state.audio_data, format='audio/mpeg')
    elif mode == 6: 
        st.markdown(f"### {q['translation']}")
        st.write("請重組句子：")
    elif mode == 7: 
        st.markdown(f"### {q['kanji']}")
        st.caption(f"意思: {q['meaning']}")
    elif mode == 8: 
        st.write("請聽單字：")
        if st.session_state.audio_data: st.audio(st.session_state.audio_data, format='audio/mpeg')
    elif mode == 9: 
        st.markdown(f"### {q['sentence']}")
        st.caption(f"意思: {q['translation']}")
    elif mode == 10: st.markdown(f"### {q['kanji']}")

st.divider()

has_answered = st.session_state.feedback is not None

# 作答區
if mode in [1, 2, 3, 4, 8]: # 選擇題
    c1, c2 = st.columns(2)
    for i, opt in enumerate(st.session_state.options):
        (c1 if i%2==0 else c2).button(opt, key=f"opt_{i}", on_click=check_answer, args=(opt,), disabled=has_answered, use_container_width=True)

elif mode in [9, 10]: # 口說
    if not has_answered:
        col_rec, col_msg = st.columns([1, 3])
        with col_rec:
            audio_blob = mic_recorder(start_prompt="🎙️ 録音", stop_prompt="⏹️ 停止", key='mic', format="wav")
        with col_msg:
            if audio_blob:
                res = transcribe_audio_bytes(audio_blob['bytes'])
                st.write(f"👂: {res}")
                check_answer(res)
                st.rerun()
        if st.button("😶 Skip"): 
            pick_new_question()
            st.rerun()

elif mode == 6: # 重組
    # (省略部分重複代碼，邏輯同原版，只需確保 selected_indices 運作正常)
    # 顯示已選
    with st.container(border=True):
        ids = st.session_state.selected_indices
        if not ids: st.write("*(點擊下方字卡)*")
        else:
            cols = st.columns(6)
            lookup = {item['id']: item['text'] for item in st.session_state.shuffled_parsing}
            for i, idx in enumerate(ids):
                cols[i%6].button(lookup[idx], key=f"sel_{idx}", on_click=deselect_block, args=(idx,), disabled=has_answered)
    
    st.write("⬇️ 待選區")
    avail = [b for b in st.session_state.shuffled_parsing if b['id'] not in ids]
    if avail:
        cols = st.columns(6)
        for i, b in enumerate(avail):
            cols[i%6].button(b['text'], key=f"avail_{b['id']}", on_click=select_block, args=(b['id'],), disabled=has_answered)
    
    if st.button("🚀 送出", type="primary", disabled=(has_answered or not ids)):
        submit_parsing()
        st.rerun()

else: # 打字
    ph = "請輸入平假名..." if mode == 7 else "請輸入日文..."
    with st.form("ans_form", clear_on_submit=True):
        val = st.text_input("Answer:", placeholder=ph, disabled=has_answered)
        if st.form_submit_button("送出"):
            check_answer(val)
            st.rerun()

# 回饋區
# --- 回饋區 (修改版) ---
if st.session_state.feedback:
    fb = st.session_state.feedback
    
    # 1. 顯示答題結果 (綠色/紅色橫幅)
    if fb['type'] == 'success': 
        st.success(fb['msg'], icon="✅")
    else: 
        st.error(fb['msg'], icon="❌")
    
    # 2. 顯示完整詳解 (日文 + 中文 + 音檔)
    with st.container(border=True):
        st.caption("📖 題目詳解")
        
        q_item = st.session_state.current_q
        
        # 根據題目類型顯示不同資訊
        col_text, col_audio = st.columns([3, 1])
        
        with col_text:
            if q_item['type'] == 'sentence':
                st.markdown(f"**🇯🇵 日文：**\n### {q_item['sentence']}")
                st.markdown(f"**🇹🇼 中文：** {q_item['translation']}")
                # 如果有 parsing 資料也可以顯示，沒有則略過
                if q_item.get('parsing'):
                    st.caption(f"結構: {' | '.join(q_item['parsing'])}")
            else:
                # 單字題型
                st.markdown(f"**🇯🇵 單字：**\n### {q_item['kanji']}")
                st.markdown(f"**🗣️ 讀音：** {q_item['reading']}")
                st.markdown(f"**🇹🇼 意思：** {q_item['meaning']}")

        with col_audio:
            if st.session_state.audio_data:
                st.write("🔊 發音")
                st.audio(st.session_state.audio_data, format='audio/mpeg')

    # 3. 下一題按鈕
    st.button("👉 下一題", on_click=pick_new_question, type="primary", use_container_width=True)