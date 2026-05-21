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
    current_product = None
    
    # เพิ่มเครื่องหมายขีด (-) และบวก (+) เข้าไปในกลุ่มที่ 4 (Grade) เพื่อให้ครอบคลุมชื่อเกรดทุกรูปแบบ
    pattern_product = re.compile(r'([A-Za-z][A-Za-z0-9\(\)\#\-\_]*)\s*/\s*([A-Za-z0-9\(\)\#\-\_\.]+)\s*/\s*([0-9\.\*\-\+]*)\s*/\s*([A-Za-z0-9\(\)\@\s\_\.\-\+]+)')

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue
                
            lines = text.split('\n')
            for line in lines:
                line_str = line.strip()
                
                match = pattern_product.search(line_str)
                if match:
                    stone_abbr = match.group(1).strip()
                    # ระบบตรวจสอบตัวย่อ ถ้าตรงกับใน Excel จะสลับเป็นชื่อเต็ม
                    stone = mapping_dict.get(stone_abbr, stone_abbr)
                    
                    cut = match.group(2).strip()
                    size = match.group(3).strip()
                    grade = match.group(4).strip()
                    
                    grade = re.split(r'\s{2,}', grade)[0]
                    grade = re.sub(r'\s+[\d\.]+$', '', grade).strip()
                    
                    current_product = (stone, cut, size, grade)
                
                if current_product and 'Total Inventory' in line_str:
                    negatives = re.findall(r'-\d+', line_str)
                    if negatives:
                        bl_value = abs(int(negatives[-1]))
                        stone, cut, size, grade = current_product
                        
                        if grade.upper().startswith('AA'):
                            dict_aa[(stone, cut, size, grade)] += bl_value
                        else:
                            dict_non_aa[(stone, cut, size, grade)] += bl_value
                            
                        current_product = None

    data_aa = [{'Stone': k[0], 'Cut': k[1], 'Size': k[2], 'PCS': v, 'Grade': k[3], 'Supplier': ''} for k, v in dict_aa.items()]
    data_non_aa = [{'Stone': k[0], 'Cut': k[1], 'Size': k[2], 'PCS': v, 'Grade': k[3], 'Supplier': ''} for k, v in dict_non_aa.items()]
    
    df_aa = pd.DataFrame(data_aa)
    df_non_aa = pd.DataFrame(data_non_aa)
    
    if not df_aa.empty:
        df_aa = df_aa.sort_values(by=['Stone', 'Cut', 'Size']).reset_index(drop=True)
    if not df_non_aa.empty:
        df_non_aa = df_non_aa.sort_values(by=['Stone', 'Cut', 'Size']).reset_index(drop=True)
        
    return df_aa, df_non_aa

# ส่วนติดต่อผู้ใช้ (UI)
st.write("---")
st.subheader("1. ฐานข้อมูลแปลงชื่อพลอย (ไม่บังคับ)")
st.write("อัปโหลดไฟล์ Excel ที่มีตัวย่อในคอลัมน์แรก และชื่อเต็มในคอลัมน์สอง")
mapping_file = st.file_uploader("อัปโหลดไฟล์ Excel แปลงชื่อ", type=["xlsx", "xls"])

mapping_dict = {}
if mapping_file is not None:
    try:
        df_map = pd.read_excel(mapping_file)
        # ดึงข้อมูลจากคอลัมน์ A (ตัวย่อ) และคอลัมน์ B (ชื่อเต็ม) มาเป็นเงื่อนไขแปลงคำ
        mapping_dict = dict(zip(df_map.iloc[:, 0].astype(str).str.strip(), df_map.iloc[:, 1].astype(str).str.strip()))
        st.success(f"โหลดข้อมูลสำเร็จ: พบรายชื่อพลอย {len(mapping_dict)} รายการ")
    except Exception as e:
        st.error("ไม่สามารถอ่านไฟล์ Excel ได้")

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
