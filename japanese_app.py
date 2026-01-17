import streamlit as st
import pandas as pd
import random
import os
import json
import re
import difflib
import asyncio
import edge_tts
from io import BytesIO
import speech_recognition as sr
from streamlit_mic_recorder import mic_recorder
import pykakasi 
from datetime import datetime, timedelta

# --- 設定區 ---
DATA_FILENAME = 'Phrases.xlsx'
MISTAKE_FILENAME = 'jp_mistakes.json'
SRS_DB_FILENAME = 'srs_progress.json' # [新增] 儲存記憶進度
TEMP_AUDIO_FILE = "temp_jp_voice.mp3"

# --- 1. 資料處理與載入 ---

@st.cache_data
def load_data():
    if not os.path.exists(DATA_FILENAME): return [], [], {}, []
    try:
        try:
            df = pd.read_excel(DATA_FILENAME).fillna("")
        except:
            df = pd.read_csv(DATA_FILENAME).fillna("")
            
        sentence_data = [] 
        vocab_data = []    
        group_map = {}     
        
        all_sentence_translations = []
        all_vocab_meanings = []

        # 取得今天日期字串，若 Excel Time 空白則預設為今天
        default_date = datetime.now().strftime("%Y-%m-%d")

        for _, row in df.iterrows():
            # 1. 解析句子資料
            s_ja = str(row.get('Sentence', '')).strip()
            s_ch = str(row.get('Translation', '')).strip()
            gid  = str(row.get('Group', '')).strip()
            # [新增] 讀取 Time 欄位
            time_str = str(row.get('Time', default_date)).strip()
            if not time_str: time_str = default_date # 防呆
            
            # 嘗試正規化日期格式 (簡單處理 YYYY-MM-DD)
            try:
                # 如果 Excel 讀入是 datetime 物件
                if isinstance(row.get('Time'), datetime):
                    time_str = row.get('Time').strftime("%Y-%m-%d")
            except:
                pass

            # [修正] 處理 Parsing 欄位：將全形＋轉為半形+，再進行切割
            parsing_raw = str(row.get('Parsing', '')).strip()
            parsing_raw = parsing_raw.replace('＋', '+') 
            
            if s_ja and s_ch:
                item = {
                    "type": "sentence",
                    "sentence": s_ja,
                    "translation": s_ch,
                    "group": gid,
                    "parsing": [p.strip() for p in parsing_raw.split('+') if p.strip()],
                    "start_date": time_str # 啟用日期
                }
                sentence_data.append(item)
                all_sentence_translations.append(s_ch)
                
                if gid:
                    if gid not in group_map: group_map[gid] = []
                    if s_ja not in group_map[gid]: group_map[gid].append(s_ja)

            # 2. 解析單字資料
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
                            
                        v_item = {
                            "type": "vocab",
                            "kanji": kanji.strip(),
                            "reading": reading.strip(),
                            "meaning": m_items[i],
                            "start_date": time_str # 單字共用該行的時間
                        }
                        vocab_data.append(v_item)
                        all_vocab_meanings.append(m_items[i])

        return sentence_data, vocab_data, group_map, (all_sentence_translations, all_vocab_meanings)
    except Exception as e:
        st.error(f"讀取資料失敗: {e}")
        return [], [], {}, []

# --- JSON 存取 ---
def load_json(filename):
    if not os.path.exists(filename): return {} if 'srs' in filename else []
    try:
        with open(filename, 'r', encoding='utf-8') as f: return json.load(f)
    except: return {} if 'srs' in filename else []

def save_json(filename, data):
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except: pass

# --- SRS 核心演算法 ---
def update_srs_status(key, is_correct):
    srs_db = st.session_state.srs_db
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    # 建立初始紀錄
    if key not in srs_db:
        srs_db[key] = {
            "next_review": today_str,
            "interval": 0,
            "reps": 0
        }
    
    record = srs_db[key]
    
    if is_correct:
        # 答對：拉長間隔 (Fibonacci-like: 1, 3, 6, 14, 30...)
        if record["interval"] == 0:
            record["interval"] = 1
        elif record["interval"] == 1:
            record["interval"] = 3
        else:
            record["interval"] = int(record["interval"] * 2.2) # 係數可調
            
        record["reps"] += 1
    else:
        # 答錯：重置，明天立即複習
        record["interval"] = 0
        record["reps"] = 0
    
    # 計算下次複習日
    next_date = datetime.now() + timedelta(days=record["interval"])
    record["next_review"] = next_date.strftime("%Y-%m-%d")
    
    srs_db[key] = record
    save_json(SRS_DB_FILENAME, srs_db)
    
    return record["interval"], record["next_review"]

# --- Edge-TTS ---
async def _edge_tts_save(text, voice="ja-JP-KeitaNeural"):
    try:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(TEMP_AUDIO_FILE)
        return True
    except Exception as e:
        print(f"EdgeTTS Error: {e}")
        return False

def get_audio_bytes(text):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success = loop.run_until_complete(_edge_tts_save(text))
        
        if success and os.path.exists(TEMP_AUDIO_FILE):
            with open(TEMP_AUDIO_FILE, "rb") as f:
                audio_bytes = f.read()
            return audio_bytes
        return None
    except Exception as e:
        st.error(f"語音生成失敗: {e}")
        return None

# --- 輔助函式 ---

def get_hiragana(text):
    kks = pykakasi.kakasi()
    result = kks.convert(text)
    return "".join([item['hira'] for item in result])

def generate_diff(user_text, target_text):
    s = difflib.SequenceMatcher(None, user_text, target_text)
    html = []
    for opcode, a0, a1, b0, b1 in s.get_opcodes():
        if opcode == 'equal':
            html.append(f"<span style='color:green; font-weight:bold'>{target_text[b0:b1]}</span>")
        elif opcode == 'insert':
            html.append(f"<span style='color:red; text-decoration:underline; background-color:#ffe6e6'>[{target_text[b0:b1]}]</span>")
        elif opcode == 'delete':
             html.append(f"<span style='color:gray; text-decoration:line-through'>{user_text[a0:a1]}</span>")
        elif opcode == 'replace':
            html.append(f"<span style='color:gray; text-decoration:line-through'>{user_text[a0:a1]}</span>")
            html.append(f"<span style='color:red; background-color:#ffe6e6'>[{target_text[b0:b1]}]</span>")
    return "".join(html)

def transcribe_audio_bytes(audio_bytes):
    r = sr.Recognizer()
    try:
        with sr.AudioFile(BytesIO(audio_bytes)) as source:
            audio_data = r.record(source)
            text = r.recognize_google(audio_data, language='ja-JP')
            return text
    except sr.UnknownValueError: return "Not Recognized"
    except sr.RequestError: return "API Error"
    except Exception as e: return str(e)

# --- 2. 狀態初始化 ---

if 'initialized' not in st.session_state:
    s_data, v_data, g_map, pools = load_data()
    st.session_state.sentence_data = s_data
    st.session_state.vocab_data = v_data
    st.session_state.group_map = g_map
    st.session_state.trans_pool = pools[0]
    st.session_state.meaning_pool = pools[1]
    
    st.session_state.mistakes = load_json(MISTAKE_FILENAME)
    st.session_state.srs_db = load_json(SRS_DB_FILENAME) # 載入 SRS 進度
    
    st.session_state.current_q = None
    st.session_state.mode = None
    st.session_state.is_review = False
    st.session_state.feedback = None
    st.session_state.audio_data = None
    st.session_state.user_audio_bytes = None
    st.session_state.options = []
    
    # Mode 6
    st.session_state.shuffled_parsing = [] 
    st.session_state.selected_indices = [] 
    
    st.session_state.initialized = True

# --- 3. 核心邏輯 (智慧選題) ---

def pick_new_question():
    st.session_state.is_review = False
    st.session_state.selected_indices = [] 
    st.session_state.shuffled_parsing = []

    today_str = datetime.now().strftime("%Y-%m-%d")
    srs_db = st.session_state.srs_db
    mistakes = st.session_state.mistakes

    # --- 1. 篩選候選池 ---
    # 我們將題目分為三類：
    # A. Due (到期): 在 SRS 系統中，且 next_review <= 今天
    # B. New (新題): 不在 SRS 系統中，且 Excel Time <= 今天
    # C. Other (其他): 尚未到期的題目 (如果上述都沒了，才拿來練習)
    
    due_items = []
    new_items = []
    
    # 檢查句子
    for item in st.session_state.sentence_data:
        key = item['sentence']
        if key in srs_db:
            if srs_db[key]['next_review'] <= today_str:
                due_items.append(item)
        else:
            if item['start_date'] <= today_str:
                new_items.append(item)
                
    # 檢查單字
    for item in st.session_state.vocab_data:
        key = item['kanji']
        if key in srs_db:
            if srs_db[key]['next_review'] <= today_str:
                due_items.append(item)
        else:
            if item['start_date'] <= today_str:
                new_items.append(item)

    # --- 2. 決定出題優先級 ---
    q_item = None
    priority_msg = ""

    # Priority 1: 到期複習 (Due)
    if due_items:
        q_item = random.choice(due_items)
        st.session_state.is_review = True
        priority_msg = "🔥 今日到期 (SRS)"
    
    # Priority 2: 錯題本 (Mistakes) - 如果沒有到期的，就清錯題
    elif mistakes and random.random() < 0.7: # 給錯題高一點的機率
        target_key = random.choice(mistakes)
        # 找句子
        q_item = next((i for i in st.session_state.sentence_data if i['sentence'] == target_key), None)
        # 找單字
        if not q_item:
            q_item = next((i for i in st.session_state.vocab_data if i['kanji'] == target_key), None)
        
        # 找不到資料(可能Excel刪了)，清理錯題
        if not q_item:
            mistakes.remove(target_key)
            save_json(MISTAKE_FILENAME, mistakes)
            pick_new_question()
            return
        
        st.session_state.is_review = True
        priority_msg = "💀 錯題複習"

    # Priority 3: 新題目 (New)
    elif new_items:
        q_item = random.choice(new_items)
        priority_msg = "✨ 新題目 (Today's New)"

    # Priority 4: 隨機鞏固 (如果今天任務都做完了)
    else:
        all_pool = st.session_state.sentence_data + st.session_state.vocab_data
        if all_pool:
            q_item = random.choice(all_pool)
            priority_msg = "🎲 隨機練習 (今日任務已清空)"
        else:
            st.error("沒有資料！請檢查 Excel 內容。")
            return

    st.session_state.priority_msg = priority_msg # 存下來顯示用

    # --- 3. 決定模式 ---
    if q_item['type'] == 'sentence':
        # 檢查是否能用 Mode 4
        can_use_mode4 = False
        if q_item['group'] in st.session_state.group_map:
             if len(st.session_state.group_map[q_item['group']]) >= 2:
                 can_use_mode4 = True
        
        available_modes = [1, 2, 3, 5, 6, 9]
        if can_use_mode4: available_modes.append(4)
        mode = random.choice(available_modes)
    else:
        # 單字
        mode = random.choice([7, 8, 10])

    setup_question(q_item, mode)

def setup_question(q_item, mode):
    st.session_state.current_q = q_item
    st.session_state.mode = mode
    st.session_state.feedback = None
    st.session_state.audio_data = None
    st.session_state.user_audio_bytes = None
    st.session_state.options = []
    
    is_vocab_mode = mode in [7, 8, 10]

    # Audio
    if mode in [3, 5, 8, 9, 10]:
        text_to_speak = q_item['kanji'] if is_vocab_mode else q_item['sentence']
        st.session_state.audio_data = get_audio_bytes(text_to_speak)
        
    # Options
    if mode in [1, 2, 3, 4, 8]:
        correct = ""
        pool = []
        
        if mode == 1 or mode == 3:
            correct = q_item['translation']
            pool = st.session_state.trans_pool
            distractors = random.sample([x for x in pool if x != correct], 3)
        elif mode == 2:
            correct = q_item['sentence']
            pool = [i['sentence'] for i in st.session_state.sentence_data]
            distractors = random.sample([x for x in pool if x != correct], 3)
        elif mode == 8:
            correct = q_item['meaning']
            pool = st.session_state.meaning_pool
            distractors = random.sample([x for x in pool if x != correct], 3)
        elif mode == 4:
            current_gid = q_item['group']
            same_group_sentences = st.session_state.group_map[current_gid]
            correct = random.choice([s for s in same_group_sentences if s != q_item['sentence']])
            
            all_groups = list(st.session_state.group_map.keys())
            other_groups = [g for g in all_groups if g != current_gid]
            
            distractors = []
            if len(other_groups) >= 3:
                selected_other_groups = random.sample(other_groups, 3)
                for og in selected_other_groups:
                    distractors.append(random.choice(st.session_state.group_map[og]))
            else:
                all_other_sentences = []
                for og in other_groups:
                    all_other_sentences.extend(st.session_state.group_map[og])
                distractors = random.sample(all_other_sentences, min(3, len(all_other_sentences)))

        final_opts = distractors + [correct]
        random.shuffle(final_opts)
        st.session_state.options = final_opts
        
    # Parsing (Mode 6)
    if mode == 6:
        if not q_item['parsing']:
            raw_parts = [q_item['sentence']]
        else:
            raw_parts = q_item['parsing'].copy()
            
        indexed_parts = [{'id': i, 'text': t} for i, t in enumerate(raw_parts)]
        random.shuffle(indexed_parts)
        st.session_state.shuffled_parsing = indexed_parts
        st.session_state.selected_indices = []

# --- Mode 6 互動邏輯 ---
def select_block(idx):
    if idx not in st.session_state.selected_indices:
        st.session_state.selected_indices.append(idx)

def deselect_block(idx):
    if idx in st.session_state.selected_indices:
        st.session_state.selected_indices.remove(idx)

def submit_parsing_answer():
    indices = st.session_state.selected_indices
    lookup = {item['id']: item['text'] for item in st.session_state.shuffled_parsing}
    user_sentence = "".join([lookup[i] for i in indices])
    check_answer(user_sentence)


def check_answer(user_input):
    if st.session_state.feedback is not None: return

    item = st.session_state.current_q
    mode = st.session_state.mode
    if not item: return

    user_clean = str(user_input).replace(" ", "").replace("　", "")
    
    target = ""
    display_correct = ""
    
    if mode == 1 or mode == 3:
        target = item['translation']
        display_correct = target
    elif mode == 8:
        target = item['meaning']
        display_correct = target
    elif mode == 2:
        target = item['sentence']
        display_correct = target
    elif mode == 4:
        gid = item['group']
        group_members = st.session_state.group_map.get(gid, [])
        target = f"Group {gid} 的近義句"
        display_correct = f"<br>".join([f"🔸 {s}" for s in group_members if s != item['sentence']])
    elif mode == 7:
        target = item['reading']
        display_correct = target
    else:
        target = item['kanji'] if (mode == 10) else item['sentence']
        display_correct = target

    is_correct_flag = False
    
    # === 比對邏輯 ===
    if mode == 4:
        is_correct_flag = (user_input in st.session_state.group_map.get(item['group'], []))
        
    elif mode in [1, 2, 3, 8]:
        is_correct_flag = (user_clean == str(target).replace(" ", ""))
        
    else:
        def clean_chars(t): 
            return re.sub(r'[。、？！\?!\s　]', '', str(t))
        
        user_hira = get_hiragana(clean_chars(user_input))
        target_hira = get_hiragana(clean_chars(target))
        is_correct_flag = (user_hira == target_hira)

    # === [關鍵] 更新 SRS 與 錯題本 ===
    key = item['sentence'] if item['type'] == 'sentence' else item['kanji']
    
    # 1. 更新記憶曲線
    new_interval, next_review_date = update_srs_status(key, is_correct_flag)
    
    # 2. 更新錯題本 (答錯一定進錯題本，答對則移出)
    if is_correct_flag:
        if key in st.session_state.mistakes:
            st.session_state.mistakes.remove(key)
            save_json(MISTAKE_FILENAME, st.session_state.mistakes)
    else:
        if key not in st.session_state.mistakes:
            st.session_state.mistakes.append(key)
            save_json(MISTAKE_FILENAME, st.session_state.mistakes)

    # 詳細回饋
    if item['type'] == 'sentence':
        detail_jp = item['sentence']
        detail_ch = item['translation']
    else:
        detail_jp = f"{item['kanji']} ({item['reading']})"
        detail_ch = item['meaning']

    detail_html = f"""
    \n🇯🇵 日文： {detail_jp}
    \n🇹🇼 中文： {detail_ch}
    \n📅 下次複習: {next_review_date} (間隔: {new_interval} 天)
    """

    if is_correct_flag:
        msg = "正解！答對了！ 🎉"
        msg += detail_html
        st.session_state.feedback = {"type": "success", "msg": msg}
    else:
        msg_header = f"❌ 残念... (答錯了)"
        if mode == 4:
             msg_body = f"<br>正確的近義句選項是:<br> **{display_correct}**"
        else:
             msg_body = f"<br>正確答案: **{display_correct}**"
        
        if mode not in [1, 2, 3, 4, 8]: 
            diff = generate_diff(user_input, str(target))
            msg_body += f"<br>差異: {diff}"
        
        full_msg = msg_header + msg_body + detail_html
        st.session_state.feedback = {"type": "error", "msg": full_msg}

    speak_text = item['kanji'] if (mode in [7,8,10]) else item['sentence']
    st.session_state.audio_data = get_audio_bytes(speak_text)

# --- 4. 介面佈局 ---

st.set_page_config(page_title="日本語特訓", page_icon="🇯🇵")

with st.sidebar:
    st.header("🧠 記憶中樞 (SRS)")
    
    # 計算統計數據
    today_str = datetime.now().strftime("%Y-%m-%d")
    srs_db = st.session_state.srs_db
    
    # 計算今日到期數
    due_count = sum(1 for k, v in srs_db.items() if v['next_review'] <= today_str)
    
    # 計算今日新單字 (Excel Time <= Today 且不在 SRS DB 中)
    # 這邊簡單估算，實際遍歷比較準
    new_count = 0
    all_keys = set(srs_db.keys())
    for item in st.session_state.sentence_data:
        if item['sentence'] not in all_keys and item['start_date'] <= today_str:
            new_count += 1
    for item in st.session_state.vocab_data:
        if item['kanji'] not in all_keys and item['start_date'] <= today_str:
            new_count += 1

    st.metric("🔥 今日需複習 (Due)", f"{due_count} 題")
    st.metric("✨ 今日新進度 (New)", f"{new_count} 題")
    st.metric("💀 錯題本 (Mistakes)", f"{len(st.session_state.mistakes)} 題")
    
    with st.expander("管理錯題"):
        if st.session_state.mistakes:
            st.write(st.session_state.mistakes)
            if st.button("清空錯題本"):
                st.session_state.mistakes = []
                save_json(MISTAKE_FILENAME, [])
                st.rerun()
        else:
            st.write("目前沒有錯題！")
            
    st.divider()
    if st.button("🔄 重載系統"):
        st.cache_data.clear()
        st.session_state.initialized = False
        st.rerun()

st.title("🇯🇵 日本語智慧特訓")
st.caption("基於遺忘曲線的 SRS 學習系統")

if st.session_state.current_q is None:
    pick_new_question()

q = st.session_state.current_q
mode = st.session_state.mode

# 顯示出題原因
tag_text = st.session_state.get("priority_msg", "New")
st.info(f"{tag_text} | Mode {mode}")

col1, col2 = st.columns([1, 4])

with col2:
    if mode == 1: 
        st.markdown(f"### {q['sentence']}")
    elif mode == 2: 
        st.markdown(f"### {q['translation']}")
    elif mode == 3: 
        st.write("請聽音檔：")
        if st.session_state.audio_data:
            st.audio(st.session_state.audio_data, format='audio/mpeg')
    elif mode == 4: 
        st.subheader(f"題目: {q['sentence']}")
        st.write("👉 請選出意思最相近（同群組）的句子：")
    elif mode == 5: 
        st.write("請聽音檔並寫下來：")
        if st.session_state.audio_data:
            st.audio(st.session_state.audio_data, format='audio/mpeg')
    elif mode == 6: 
        st.markdown(f"### {q['translation']}")
        st.write("請重組句子：")
    elif mode == 7: 
        st.markdown(f"### {q['kanji']}")
        st.caption(f"意思: {q['meaning']}")
    elif mode == 8: 
        st.write("請聽單字：")
        if st.session_state.audio_data:
            st.audio(st.session_state.audio_data, format='audio/mpeg')
    elif mode == 9: 
        st.markdown(f"### {q['sentence']}")
        st.caption(f"意思: {q['translation']}")
    elif mode == 10: 
        st.markdown(f"### {q['kanji']}")
        # st.caption(f"讀音: {q['reading']} | 意思: {q['meaning']}")

st.divider()

has_answered = st.session_state.feedback is not None

# A. 選擇題
if mode in [1, 2, 3, 4, 8]:
    st.write("請選擇:")
    opts = st.session_state.options
    c1, c2 = st.columns(2)
    for i, opt in enumerate(opts):
        if i % 2 == 0:
            c1.button(opt, key=f"opt_{i}", on_click=check_answer, args=(opt,), disabled=has_answered, use_container_width=True)
        else:
            c2.button(opt, key=f"opt_{i}", on_click=check_answer, args=(opt,), disabled=has_answered, use_container_width=True)

# B. 口說
elif mode in [9, 10]:
    if not has_answered:
        col_rec, col_msg = st.columns([1, 3])
        with col_rec:
            audio_blob = mic_recorder(start_prompt="🎙️ 録音", stop_prompt="⏹️ 停止", key='mic', format="wav")
        
        with col_msg:
            if audio_blob:
                st.session_state.user_audio_bytes = audio_blob['bytes']
                st.write("🔄 辨識中...")
                res = transcribe_audio_bytes(audio_blob['bytes'])
                if res == "Not Recognized": st.warning("聽不清楚")
                elif res == "API Error": st.error("連線錯誤")
                else:
                    st.success(f"👂: {res}")
                    check_answer(res)
                    st.rerun()
        st.markdown("")
        if st.button("😶 跳過 (Skip)"):
            pick_new_question()
            st.rerun()
    else:
        st.info("錄音結束。")

# C. 互動式重組 (Mode 6)
elif mode == 6:
    all_blocks = st.session_state.shuffled_parsing
    selected_ids = st.session_state.selected_indices
    lookup = {item['id']: item['text'] for item in all_blocks}
    
    st.write("⬇️ **點擊字卡來移除 (復原)**")
    with st.container(border=True):
        if not selected_ids:
            st.write("*(請從下方點選字卡...)*")
        else:
            rows = [selected_ids[i:i + 6] for i in range(0, len(selected_ids), 6)]
            for row_ids in rows:
                cols = st.columns(6)
                for i, idx in enumerate(row_ids):
                    with cols[i]:
                        st.button(lookup[idx], key=f"ans_{idx}", on_click=deselect_block, args=(idx,), disabled=has_answered)

    st.markdown("<br>", unsafe_allow_html=True)

    st.write("⬇️ **點擊字卡來選擇**")
    available_blocks = [b for b in all_blocks if b['id'] not in selected_ids]
    
    if available_blocks:
        rows_avail = [available_blocks[i:i + 6] for i in range(0, len(available_blocks), 6)]
        for row_items in rows_avail:
            cols_opt = st.columns(6)
            for i, block in enumerate(row_items):
                with cols_opt[i]:
                    st.button(block['text'], key=f"pool_{block['id']}", on_click=select_block, args=(block['id'],), disabled=has_answered)
    
    st.markdown("---")
    
    if st.button("🚀 送出答案", type="primary", disabled=(has_answered or len(selected_ids)==0)):
        submit_parsing_answer()
        st.rerun()

# D. 一般打字
else:
    placeholder = "請輸入日文..."
    if mode == 7: placeholder = "請輸入平假名..."
    
    with st.form(key='ans_form', clear_on_submit=True):
        user_val = st.text_input("Answer:", placeholder=placeholder, disabled=has_answered)
        submitted = st.form_submit_button("送出", disabled=has_answered)
        
    if submitted:
        check_answer(user_val)
        st.rerun()

# --- 回饋顯示區 ---
if st.session_state.feedback:
    fb = st.session_state.feedback
    
    if fb['type'] == 'success': 
        st.success(fb['msg'], icon="✅")
    else: 
        st.markdown(fb['msg'], unsafe_allow_html=True)
    
    if st.session_state.audio_data:
        st.write("🔊 標準發音:")
        st.audio(st.session_state.audio_data, format='audio/mpeg')
        
    if st.session_state.user_audio_bytes:
        st.write("🎤 你的發音:")
        st.audio(st.session_state.user_audio_bytes, format='audio/wav')
        
    st.markdown("---")
    st.button("👉 下一題 (Next)", on_click=pick_new_question, type="primary")