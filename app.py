import streamlit as st
import pdfplumber
import pandas as pd
import re
from collections import defaultdict
import io

# ตั้งค่าหน้าเว็บ
st.set_page_config(page_title="OSD Stone Extractor", page_icon="💎")
st.title("💎 ระบบแยกข้อมูล OSD Stone")
st.write("อัปโหลดไฟล์รายงาน PDF เพื่อแยกรายการพลอยที่ไม่พอในสต็อก")

def process_pdf(pdf_file_obj):
    dict_aa = defaultdict(int)
    dict_non_aa = defaultdict(int)
    current_product = None
    
    pattern_product = re.compile(r'([A-Za-z][A-Za-z0-9\(\)\#\-\_\s]*)\s*/\s*([A-Za-z0-9\(\)\#\-\_\.\s]+)\s*/\s*([0-9\.\*\-\+\s]*)\s*/\s*([A-Za-z0-9\(\)\@\s\_\.]+)')

    with pdfplumber.open(pdf_file_obj) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue
                
            raw_lines = text.split('\n')
            lines = []
            
            for r in raw_lines:
                r = r.strip()
                if not r: continue
                if lines and (r.startswith('/') or r.startswith(')') or r.startswith('*')):
                    lines[-1] += r
                elif lines and (lines[-1].endswith('/') or lines[-1].endswith('(') or lines[-1].endswith('*')):
                    lines[-1] += r
                else:
                    lines.append(r)

            for line_str in lines:
                match = pattern_product.search(line_str)
                if match:
                    stone = match.group(1).strip()
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

    data_aa = [{'Stone': k[0], 'Cut': k[1], 'Size': k[2], 'PCS': v, 'Grade': k[3]} for k, v in dict_aa.items()]
    data_non_aa = [{'Stone': k[0], 'Cut': k[1], 'Size': k[2], 'PCS': v, 'Grade': k[3]} for k, v in dict_non_aa.items()]
    
    df_aa = pd.DataFrame(data_aa)
    df_non_aa = pd.DataFrame(data_non_aa)
    
    if not df_aa.empty:
        df_aa = df_aa.sort_values(by=['Stone', 'Cut', 'Size']).reset_index(drop=True)
    if not df_non_aa.empty:
        df_non_aa = df_non_aa.sort_values(by=['Stone', 'Cut', 'Size']).reset_index(drop=True)
        
    return df_aa, df_non_aa

# ส่วนติดต่อผู้ใช้ (UI) บนหน้าเว็บ
uploaded_file = st.file_uploader("1. อัปโหลดไฟล์ PDF", type="pdf")

if uploaded_file is not None:
    if st.button("2. กดรันเพื่อดึงข้อมูล"):
        with st.spinner("กำลังอ่านและประมวลผลไฟล์..."):
            df_aa, df_non_aa = process_pdf(uploaded_file)
            
            # แสดงตัวอย่างตารางบนหน้าเว็บ
            st.success("ประมวลผลเสร็จสิ้น!")
            st.write(f"พบพลอยเกรด AA: {len(df_aa)} รายการ")
            st.write(f"พบพลอยเกรดอื่นๆ: {len(df_non_aa)} รายการ")
            
            # แปลงข้อมูลเป็นไฟล์ Excel ในหน่วยความจำเพื่อเตรียมดาวน์โหลด
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                if not df_aa.empty:
                    df_aa.to_excel(writer, sheet_name='AA_Grade', index=False)
                if not df_non_aa.empty:
                    df_non_aa.to_excel(writer, sheet_name='Non_AA_Grade', index=False)
            
            excel_data = output.getvalue()
            
            # สร้างปุ่มดาวน์โหลด
            st.download_button(
                label="3. กดเพื่อดาวน์โหลดไฟล์ Excel",
                data=excel_data,
                file_name="OSD_Separated_Report.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )