import io
import re
from collections import defaultdict

import pandas as pd
import streamlit as st


st.set_page_config(
    page_title="OSD Stone Extractor",
    page_icon="💎",
    layout="wide",
)
st.title("💎 ระบบแยกข้อมูล OSD Stone")
st.caption("เวอร์ชันอ่านไฟล์ดิบโดยตรง — ไม่ใช้ OCR และไม่อ่านจาก PDF")


STONE_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9()#_+\-]*")
SIZE_PATTERN = re.compile(r"[0-9][0-9.*+\-xX]*")


def read_text_file(uploaded_file) -> str:
    file_bytes = uploaded_file.getvalue()
    last_error = None

    for encoding in ("utf-8", "utf-8-sig", "cp874", "tis-620"):
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError as error:
            last_error = error

    raise ValueError(f"ไม่สามารถอ่านตัวอักษรในไฟล์ได้: {last_error}")


def read_mapping_file(mapping_file) -> pd.DataFrame:
    name = mapping_file.name.lower()

    if name.endswith(".csv"):
        file_bytes = mapping_file.getvalue()
        last_error = None

        for encoding in ("utf-8", "utf-8-sig", "cp874", "tis-620"):
            try:
                return pd.read_csv(
                    io.BytesIO(file_bytes),
                    sep=None,
                    engine="python",
                    encoding=encoding,
                )
            except Exception as error:
                last_error = error

        raise ValueError(f"อ่านไฟล์ CSV ไม่สำเร็จ: {last_error}")

    return pd.read_excel(mapping_file)


def parse_product_heading(line: str):
    product_text = re.split(r"\s{2,}", line.strip(), maxsplit=1)[0].strip()
    parts = [part.strip() for part in product_text.split("/")]

    if len(parts) < 4:
        return None

    stone = parts[0]
    cut = "/".join(parts[1:-2]).strip()
    size = parts[-2]
    grade = parts[-1]

    if not STONE_PATTERN.fullmatch(stone):
        return None
    if not SIZE_PATTERN.fullmatch(size):
        return None
    if not cut or not grade:
        return None

    return stone, cut, size, grade


@st.cache_data(show_spinner=False)
def process_raw_text(raw_text: str, mapping_items):
    mapping_dict = dict(mapping_items)
    mapping_upper = {str(key).upper(): value for key, value in mapping_dict.items()}

    grouped_aa = defaultdict(int)
    grouped_non_aa = defaultdict(int)

    current_product = None
    product_count = 0
    total_inventory_count = 0
    negative_bl_count = 0
    errors = []

    for line_number, raw_line in enumerate(raw_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        if "Total Inventory:" in line:
            total_inventory_count += 1

            bl_match = re.search(r"(-?\d+)\s*$", line)
            if not bl_match:
                errors.append(
                    {
                        "บรรทัด": line_number,
                        "ปัญหา": "อ่านค่า B/L ท้ายบรรทัดไม่ได้",
                        "ข้อความ": line,
                    }
                )
                current_product = None
                continue

            bl_value = int(bl_match.group(1))

            if bl_value < 0:
                negative_bl_count += 1

                if current_product is None:
                    errors.append(
                        {
                            "บรรทัด": line_number,
                            "ปัญหา": "พบ B/L ติดลบ แต่ไม่พบหัวข้อสินค้าก่อนหน้า",
                            "ข้อความ": line,
                        }
                    )
                else:
                    stone, cut, size, grade = current_product
                    stone_name = mapping_dict.get(
                        stone,
                        mapping_upper.get(stone.upper(), stone),
                    )

                    key = (stone_name, cut, size, grade)
                    pcs = abs(bl_value)

                    if grade.upper().startswith("AA"):
                        grouped_aa[key] += pcs
                    else:
                        grouped_non_aa[key] += pcs

            current_product = None
            continue

        product = parse_product_heading(line)
        if product is not None:
            current_product = product
            product_count += 1

    columns = ["Stone", "Cut", "Size", "PCS", "Grade"]

    aa_data = [
        {
            "Stone": key[0],
            "Cut": key[1],
            "Size": key[2],
            "PCS": pcs,
            "Grade": key[3],
        }
        for key, pcs in grouped_aa.items()
    ]

    non_aa_data = [
        {
            "Stone": key[0],
            "Cut": key[1],
            "Size": key[2],
            "PCS": pcs,
            "Grade": key[3],
        }
        for key, pcs in grouped_non_aa.items()
    ]

    df_aa = pd.DataFrame(aa_data, columns=columns)
    df_non_aa = pd.DataFrame(non_aa_data, columns=columns)
    df_errors = pd.DataFrame(
        errors,
        columns=["บรรทัด", "ปัญหา", "ข้อความ"],
    )

    if not df_aa.empty:
        df_aa = df_aa.sort_values(
            by=["Stone", "Cut", "Size", "Grade"],
            kind="stable",
        ).reset_index(drop=True)

    if not df_non_aa.empty:
        df_non_aa = df_non_aa.sort_values(
            by=["Stone", "Cut", "Size", "Grade"],
            kind="stable",
        ).reset_index(drop=True)

    stats = {
        "product_count": product_count,
        "total_inventory_count": total_inventory_count,
        "negative_bl_count": negative_bl_count,
        "output_count": len(df_aa) + len(df_non_aa),
        "error_count": len(df_errors),
    }

    return df_aa, df_non_aa, df_errors, stats


def create_excel(df_aa: pd.DataFrame, df_non_aa: pd.DataFrame) -> bytes:
    output = io.BytesIO()

    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        workbook = writer.book

        header_format = workbook.add_format(
            {
                "bold": True,
                "border": 1,
                "align": "center",
                "valign": "vcenter",
            }
        )
        text_format = workbook.add_format({"num_format": "@", "border": 1})
        number_format = workbook.add_format({"num_format": "0", "border": 1})

        for sheet_name, dataframe in (
            ("AA_Grade", df_aa),
            ("Non_AA_Grade", df_non_aa),
        ):
            dataframe.to_excel(writer, sheet_name=sheet_name, index=False)
            worksheet = writer.sheets[sheet_name]

            worksheet.freeze_panes(1, 0)
            worksheet.autofilter(0, 0, len(dataframe), len(dataframe.columns) - 1)
            worksheet.set_row(0, 22, header_format)

            worksheet.set_column("A:A", 16, text_format)
            worksheet.set_column("B:B", 28, text_format)
            worksheet.set_column("C:C", 16, text_format)
            worksheet.set_column("D:D", 10, number_format)
            worksheet.set_column("E:E", 20, text_format)

    return output.getvalue()


st.divider()
st.subheader("1. ฐานข้อมูลแปลงชื่อพลอย (ไม่บังคับ)")
st.caption("คอลัมน์แรกเป็นตัวย่อ Stone และคอลัมน์ที่สองเป็นชื่อเต็ม")

mapping_file = st.file_uploader(
    "อัปโหลดไฟล์แปลงชื่อ",
    type=["xlsx", "xls", "csv"],
    key="mapping_file",
)

mapping_dict = {}

if mapping_file is not None:
    try:
        df_mapping = read_mapping_file(mapping_file)

        if df_mapping.shape[1] < 2:
            st.error("ไฟล์แปลงชื่อต้องมีอย่างน้อย 2 คอลัมน์")
        else:
            abbreviations = df_mapping.iloc[:, 0].astype(str).str.strip()
            full_names = df_mapping.iloc[:, 1].astype(str).str.strip()
            mapping_dict = dict(zip(abbreviations, full_names))
            st.success(f"โหลดฐานข้อมูลแปลงชื่อสำเร็จ {len(mapping_dict)} รายการ")
    except Exception as error:
        st.error(f"อ่านไฟล์แปลงชื่อไม่สำเร็จ: {error}")


st.divider()
st.subheader("2. อัปโหลดไฟล์ดิบ OSD")
st.caption(
    "รองรับไฟล์ TXT/TSV ที่คัดลอกหรือบันทึกจากรายงานต้นทาง "
    "ไม่ต้องแปลงเป็น PDF"
)

raw_file = st.file_uploader(
    "อัปโหลดไฟล์ดิบ",
    type=["txt", "tsv"],
    key="raw_report",
)

pasted_text = st.text_area(
    "หรือวางข้อความดิบตรงนี้",
    height=140,
    placeholder="วางข้อมูล Outstanding Stones due date ตรงนี้…",
)

raw_text = ""

if raw_file is not None:
    try:
        raw_text = read_text_file(raw_file)
        st.success(f"โหลดไฟล์ดิบสำเร็จ: {raw_file.name}")
    except Exception as error:
        st.error(str(error))
elif pasted_text.strip():
    raw_text = pasted_text


if raw_text:
    mapping_items = tuple(sorted(mapping_dict.items()))

    with st.spinner("กำลังแยกข้อมูลจากไฟล์ดิบ…"):
        df_aa, df_non_aa, df_errors, stats = process_raw_text(
            raw_text,
            mapping_items,
        )

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("หัวข้อสินค้า", stats["product_count"])
    col2.metric("Total Inventory", stats["total_inventory_count"])
    col3.metric("B/L ติดลบ", stats["negative_bl_count"])
    col4.metric("แถวผลลัพธ์", stats["output_count"])

    counts_match = (
        stats["product_count"] == stats["total_inventory_count"]
        and stats["negative_bl_count"] == stats["output_count"]
        and stats["error_count"] == 0
    )

    if counts_match:
        st.success(
            "ตรวจสอบจำนวนครบถ้วน: หัวข้อสินค้าตรงกับ Total Inventory "
            "และไม่มีรายการ B/L ติดลบสูญหาย"
        )
    else:
        st.error(
            "จำนวนข้อมูลไม่ตรงกัน ระบบจึงยังไม่เปิดให้ดาวน์โหลด "
            "เพื่อป้องกันรายการหายหรือจับคู่ผิด"
        )

    st.divider()

    edited_df_aa = pd.DataFrame(columns=df_aa.columns)
    edited_df_non_aa = pd.DataFrame(columns=df_non_aa.columns)

    if not df_aa.empty:
        st.write(f"**พลอยเกรด AA ({len(df_aa)} รายการ)**")
        edited_df_aa = st.data_editor(
            df_aa,
            key="editor_aa",
            use_container_width=True,
            num_rows="dynamic",
        )

    if not df_non_aa.empty:
        st.write(f"**พลอยเกรดอื่น ({len(df_non_aa)} รายการ)**")
        edited_df_non_aa = st.data_editor(
            df_non_aa,
            key="editor_non_aa",
            use_container_width=True,
            num_rows="dynamic",
        )

    if not df_errors.empty:
        st.write("**รายการที่ระบบอ่านไม่ได้**")
        st.dataframe(df_errors, use_container_width=True)

    if counts_match:
        excel_data = create_excel(edited_df_aa, edited_df_non_aa)

        st.divider()
        st.download_button(
            label="3. ดาวน์โหลดไฟล์ Excel",
            data=excel_data,
            file_name="OSD_Report.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
