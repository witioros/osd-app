import streamlit as st
import pdfplumber
import pandas as pd
import re
from collections import defaultdict
import io

st.set_page_config(page_title="OSD Stone Extractor", page_icon="💎", layout="wide")
st.title("💎 ระบบแยกข้อมูล OSD Stone")

@st.cache_data
def process_pdf(pdf_bytes, mapping_dict):
    dict_aa = defaultdict(int)
    dict_non_aa = defaultdict(int)
    
    pattern_product = re.compile(r'([A-Za-z][A-Za-z0-9\(\)\#\-\_\+]*)\s*/\s*(.+?)\s*/\s*([0-9\.\*\-\+]*)\s*/\s*([A-Za-z0-9\(\)\@\s\_\.\-\+]+)')

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text(layout=True) # ใช้ layout=True เพื่อรักษารูปแบบตาราง
            if not text:
                continue
                
            lines = text.split('\n')
            
            # ระบบค้นหาแบบ 2 ทิศทาง (บน-ล่าง)
            for i, line in enumerate(lines):
                if 'Total Inventory:' in line:
                    # หาระยะค้นหา (ย้อนขึ้น 10 บรรทัด และลงไป 10 บรรทัด)
                    start_idx = max(0, i - 10)
                    end_idx = min(len(lines), i + 10)
                    search_block = lines[start_idx:end_idx]
                    
                    current_product = None
                    bl_value = 0
                    
                    # 1. หาชื่อพลอยในบล็อกนี้
                    for b_line in search_block:
                        match = pattern_product.search(b_line.strip())
                        if match:
                            stone_abbr = match.group(1).strip()
                            stone = mapping_dict.get(stone_abbr, stone_abbr)
                            cut = match.group(2).strip()
                            size = match.group(3).strip()
                            grade = match.group(4).strip()
                            grade = re.split(r'\s{2,}', grade)[0]
                            grade = re.sub(r'\s+[\d\.]+$', '', grade).strip()
                            current_product = (stone, cut, size, grade)
                            break # เจอพลอยแล้วหยุดหา
                            
                    # 2. หายอดติดลบในบรรทัดที่มีคำว่า Total Inventory หรือบรรทัดใกล้เคียง
                    if current_product:
                        for b_line in lines[i:min(len(lines), i + 5)]: # ดูลงมาจาก Total นิดหน่อย
                            negatives = re.findall(r'-\d+', b_line)
                            if negatives:
                                bl_value = abs(int(negatives[-1]))
                                break
                        
                        # บันทึกข้อมูล
                        if bl_value > 0:
                            stone, cut, size, grade = current_product
                            if grade.upper().startswith('AA'):
                                dict_aa[(stone, cut, size, grade)] += bl_value
                            else:
                                dict_non_aa[(stone, cut, size, grade)] += bl_value
                            
                            # เคลียร์ตัวแปรเพื่อป้องกันการนับซ้ำใน Total ถัดไป
                            for j in range(start_idx, end_idx):
                                if pattern_product.search(lines[j]):
                                    lines[j] = lines[j].replace(match.group(0), "PROCESSED_STONE")

    data_aa = [{'Stone': k[0], 'Cut': k[1], 'Size': k[2], 'PCS': v, 'Grade': k[3]} for k, v in dict_aa.items()]
    data_non_aa = [{'Stone': k[0], 'Cut': k[1], 'Size': k[2], 'PCS': v, 'Grade': k[3]} for k, v in dict_non_aa.items()]
    
    df_aa = pd.DataFrame(data_aa)
    df_non_aa = pd.DataFrame(data_non_aa)
    
    # รวมข้อมูลที่ซ้ำกันเผื่อระบบดึงมา 2 รอบจากหน้าต่อหน้า
    if not df_aa.empty:
        df_aa = df_aa.groupby(['Stone', 'Cut', 'Size', 'Grade'], as_index=False)['PCS'].sum()
        df_aa = df_aa.sort_values(by=['Stone', 'Cut', 'Size']).reset_index(drop=True)
    if not df_non_aa.empty:
        df_non_aa = df_non_aa.groupby(['Stone', 'Cut', 'Size', 'Grade'], as_index=False)['PCS'].sum()
        df_non_aa = df_non_aa.sort_values(by=['Stone', 'Cut', 'Size']).reset_index(drop=True)
        
    return df_aa, df_non_aa

# ส่วนติดต่อผู้ใช้ (UI)
st.write("---")
st.subheader("1. ฐานข้อมูลแปลงชื่อพลอย (ไม่บังคับ)")
st.write("อัปโหลดไฟล์ Excel หรือ CSV ที่มีตัวย่อในคอลัมน์แรก และชื่อเต็มในคอลัมน์สอง")
mapping_file = st.file_uploader("อัปโหลดไฟล์แปลงชื่อ", type=["xlsx", "xls", "csv"])

mapping_dict = {}
if mapping_file is not None:
    try:
        if mapping_file.name.lower().endswith('.csv'):
            file_bytes = mapping_file.getvalue()
            for encoding in ['utf-8', 'utf-8-sig', 'cp874', 'tis-620']:
                try:
                    df_map = pd.read_csv(io.BytesIO(file_bytes), sep=None, engine='python', encoding=encoding)
                    break
                except Exception:
                    continue
            else:
                df_map = pd.read_csv(io.BytesIO(file_bytes))
        else:
            df_map = pd.read_excel(mapping_file)
            
        mapping_dict = dict(zip(df_map.iloc[:, 0].astype(str).str.strip(), df_map.iloc[:, 1].astype(str).str.strip()))
        st.success(f"โหลดข้อมูลสำเร็จ: พบรายชื่อพลอย {len(mapping_dict)} รายการ")
    except Exception as e:
        st.error(f"ไม่สามารถอ่านไฟล์ได้: {e}")

st.write("---")
st.subheader("2. อัปโหลดไฟล์ OSD Report (PDF)")
uploaded_file = st.file_uploader("อัปโหลดไฟล์ PDF ตรงนี้", type="pdf")

if uploaded_file is not None:
    pdf_bytes = uploaded_file.read()
    df_aa, df_non_aa = process_pdf(pdf_bytes, mapping_dict)
    
    st.write("---")
    edited_df_aa = pd.DataFrame()
    edited_df_non_aa = pd.DataFrame()

    if not df_aa.empty:
        st.write(f"**พลอยเกรด AA ({len(df_aa)} รายการ)**")
        edited_df_aa = st.data_editor(df_aa, key="editor_aa", use_container_width=True)
        
    if not df_non_aa.empty:
        st.write(f"**พลอยเกรดอื่นๆ ({len(df_non_aa)} รายการ)**")
        edited_df_non_aa = st.data_editor(df_non_aa, key="editor_non_aa", use_container_width=True)
        
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        if not edited_df_aa.empty:
            edited_df_aa.to_excel(writer, sheet_name='AA_Grade', index=False)
        if not edited_df_non_aa.empty:
            edited_df_non_aa.to_excel(writer, sheet_name='Non_AA_Grade', index=False)
    
    excel_data = output.getvalue()
    
    st.write("---")
    st.download_button(
        label="3. กดเพื่อดาวน์โหลดไฟล์ Excel",
        data=excel_data,
        file_name="OSD_Report_Updated_Names.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
