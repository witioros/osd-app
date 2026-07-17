import io
import re
from collections import defaultdict

import pandas as pd
import pdfplumber
import pypdfium2 as pdfium
import pytesseract
import streamlit as st
from PIL import ImageEnhance, ImageOps


st.set_page_config(
    page_title="OSD Stone Extractor",
    page_icon="💎",
    layout="wide",
)
st.title("💎 ระบบแยกข้อมูล OSD Stone")


# -----------------------------
# เครื่องมือช่วยอ่านและแยกข้อมูล
# -----------------------------
PRODUCT_PATTERN = re.compile(
    r"^"
    r"(?P<stone>[A-Za-z][A-Za-z0-9()#_\-+]*)"
    r"\s*/\s*"
    r"(?P<cut>.+?)"
    r"\s*/\s*"
    r"(?P<size>[0-9][0-9.*xX+\-\"']*)"
    r"\s*/\s*"
    r"(?P<grade>[A-Za-z][A-Za-z0-9@()_.\-+ ]*)"
    r"$"
)


def clean_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_ocr_line(line: str, mapping_dict: dict) -> str:
    """แก้อักขระที่ OCR มักอ่านสับสน โดยไม่เปลี่ยนข้อมูลเกินจำเป็น"""
    line = clean_spaces(line)

    replacements = {
        "／": "/",
        "\\": "/",
        "|": "/",
        "｜": "/",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "×": "*",
    }
    for old, new in replacements.items():
        line = line.replace(old, new)

    # ตัดราคา/หน่วยที่มักต่อท้ายหัวข้อสินค้า
    line = re.split(r"\s{2,}|[—–]{1,}", line, maxsplit=1)[0].strip()
    line = re.sub(r"\s+-+\s+.*$", "", line).strip()

    # OCR มักอ่าน / เป็นตัว I ในตำแหน่งคั่นก่อน Size หรือ Grade
    line = re.sub(r"(?<=[A-Za-z])I(?=\d)", "/", line)
    line = re.sub(r"(?<=[0-9*.'\"])[I](?=[A-Za-z])", "/", line)

    # ถ้ามีฐานข้อมูลตัวย่อ ให้ใช้ช่วยแก้ตัวคั่นตัวแรกที่ OCR อ่านเป็น I
    upper_line = line.upper()
    for abbr in sorted(mapping_dict.keys(), key=len, reverse=True):
        abbr_text = str(abbr).strip()
        if not abbr_text:
            continue
        upper_abbr = abbr_text.upper()
        if upper_line.startswith(upper_abbr + "I"):
            line = abbr_text + "/" + line[len(abbr_text) + 1 :]
            break

    # ในช่อง Size เครื่องหมายคำพูดมักเป็นเครื่องหมายคูณ
    # เช่น 18"13*4 -> 18*13*4
    parts = [part.strip() for part in line.split("/")]
    if len(parts) >= 4:
        parts[-2] = parts[-2].replace('"', "*").replace("'", "*")
        line = "/".join(parts)

    return line.strip(" -_")


def parse_product_line(line: str, mapping_dict: dict):
    normalized = normalize_ocr_line(line, mapping_dict)

    match = PRODUCT_PATTERN.match(normalized)
    if not match:
        return None, normalized

    stone_abbr = match.group("stone").strip()
    stone = mapping_dict.get(stone_abbr, mapping_dict.get(stone_abbr.upper(), stone_abbr))

    cut = clean_spaces(match.group("cut"))
    size = clean_spaces(match.group("size")).replace('"', "*").replace("'", "*")
    grade = clean_spaces(match.group("grade"))

    # ตัดราคาหรือข้อความส่วนเกินที่อาจหลุดมาต่อท้าย Grade
    grade = re.split(r"\s{2,}", grade)[0]
    grade = re.sub(r"\s+[\d.]+\s*/\s*(?:pc|ct)\b.*$", "", grade, flags=re.I).strip()
    grade = re.sub(r"\s+[\d.]+$", "", grade).strip()

    return (stone, cut, size, grade), normalized


def find_shortage(line: str):
    """อ่านจำนวน B/L จากเลขติดลบตัวสุดท้ายในบรรทัด Total Inventory"""
    if not re.search(r"TOTAL\s+INVENTORY", line, flags=re.I):
        return None

    negatives = re.findall(r"-\s*(\d+)", line)
    if not negatives:
        return None

    return int(negatives[-1])


def extract_native_text(pdf_bytes: bytes):
    page_texts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_texts.append(page.extract_text() or "")
    return page_texts


def native_text_is_usable(page_texts) -> bool:
    joined = "\n".join(page_texts)
    inventory_count = len(re.findall(r"TOTAL\s+INVENTORY", joined, flags=re.I))
    product_like_count = len(
        re.findall(r"[A-Za-z][^\n]*/[^\n]*/[0-9][^\n]*/[A-Za-z]", joined)
    )
    return inventory_count > 0 and product_like_count > 0


def extract_pairs_from_text(page_texts, mapping_dict):
    """ใช้กับ PDF ที่มีข้อความ Unicode ปกติ"""
    pairs = []
    unparsed = []
    current_product = None
    current_raw = None

    for page_no, text in enumerate(page_texts, start=1):
        for raw_line in text.splitlines():
            line = clean_spaces(raw_line)
            if not line:
                continue

            parsed, normalized = parse_product_line(line, mapping_dict)
            if parsed:
                current_product = parsed
                current_raw = normalized

            shortage = find_shortage(line)
            if shortage is not None:
                if current_product:
                    pairs.append((*current_product, shortage))
                else:
                    unparsed.append(
                        {
                            "Page": page_no,
                            "Raw text": current_raw or "(ไม่พบหัวข้อสินค้าก่อน Total Inventory)",
                            "PCS": shortage,
                        }
                    )
                current_product = None
                current_raw = None

    return pairs, unparsed


def render_pdf_page(document, page_index: int):
    # Scale 3 ประมาณ 216 DPI: ชัดพอสำหรับ OCR และไม่หนักเกินไป
    image = document[page_index].render(scale=3.0).to_pil().convert("L")
    image = ImageOps.autocontrast(image)
    image = ImageEnhance.Contrast(image).enhance(1.25)
    return image


def looks_like_product_heading(line: str) -> bool:
    upper = line.upper()

    if not line or "TOTAL" in upper or "HTTP" in upper:
        return False
    if re.search(r"\d{1,2}/\d{1,2}/\d{4}", line):
        return False
    if not re.search(r"(?:/|I)(?:AA|AAA|A|AB|B|C)(?:@[\w\-]+)?\b", upper):
        return False

    slash_count = line.count("/") + line.count("|") + line.count("\\")
    return slash_count >= 2 or ("AA" in upper and len(line) <= 100)


def ocr_pdf(pdf_bytes: bytes, mapping_dict: dict):
    """
    OCR แบบปรับตัว:
    - ใช้ PSM 3 ก่อน เพื่อรักษาลำดับหัวข้อกับ Total Inventory
    - ใช้ PSM 11 เพิ่มเฉพาะหน้าที่หัวข้อสินค้าหายมาก
    """
    document = pdfium.PdfDocument(pdf_bytes)

    all_products = []
    all_totals = []
    product_pages = []
    total_pages = []

    for page_index in range(len(document)):
        image = render_pdf_page(document, page_index)

        text_layout = pytesseract.image_to_string(
            image,
            lang="eng",
            config="--oem 3 --psm 3",
        )

        page_products = [
            clean_spaces(line)
            for line in text_layout.splitlines()
            if looks_like_product_heading(clean_spaces(line))
        ]

        page_totals = []
        for line in text_layout.splitlines():
            shortage = find_shortage(clean_spaces(line))
            if shortage is not None:
                page_totals.append(shortage)

        # ถ้า OCR แบบรักษารูปหน้าเจอหัวข้อน้อยกว่ายอด Total มาก ให้สแกนข้อความกระจายเพิ่ม
        if page_totals and len(page_products) < max(1, int(len(page_totals) * 0.75)):
            sparse_text = pytesseract.image_to_string(
                image,
                lang="eng",
                config="--oem 3 --psm 11",
            )
            sparse_products = [
                clean_spaces(line)
                for line in sparse_text.splitlines()
                if looks_like_product_heading(clean_spaces(line))
            ]
            if len(sparse_products) > len(page_products):
                page_products = sparse_products

        all_products.extend(page_products)
        product_pages.extend([page_index + 1] * len(page_products))
        all_totals.extend(page_totals)
        total_pages.extend([page_index + 1] * len(page_totals))

    pairs = []
    unparsed = []

    pair_count = min(len(all_products), len(all_totals))
    for index in range(pair_count):
        raw_product = all_products[index]
        shortage = all_totals[index]
        parsed, normalized = parse_product_line(raw_product, mapping_dict)

        if parsed:
            pairs.append((*parsed, shortage))
        else:
            unparsed.append(
                {
                    "Page": product_pages[index],
                    "Raw text": normalized,
                    "PCS": shortage,
                }
            )

    # แสดงสิ่งที่ OCR จับได้ไม่ครบ แทนการทิ้งเงียบ ๆ
    if len(all_totals) > pair_count:
        for index in range(pair_count, len(all_totals)):
            unparsed.append(
                {
                    "Page": total_pages[index],
                    "Raw text": "(พบ Total Inventory แต่จับคู่หัวข้อสินค้าไม่ได้)",
                    "PCS": all_totals[index],
                }
            )

    if len(all_products) > pair_count:
        for index in range(pair_count, len(all_products)):
            _, normalized = parse_product_line(all_products[index], mapping_dict)
            unparsed.append(
                {
                    "Page": product_pages[index],
                    "Raw text": normalized,
                    "PCS": None,
                }
            )

    stats = {
        "pages": len(document),
        "product_headings": len(all_products),
        "inventory_rows": len(all_totals),
        "parsed_rows": len(pairs),
        "unparsed_rows": len(unparsed),
    }
    return pairs, unparsed, stats


def build_dataframes(pairs):
    dict_aa = defaultdict(int)
    dict_non_aa = defaultdict(int)

    for stone, cut, size, grade, pcs in pairs:
        key = (stone, cut, size, grade)
        if grade.upper().startswith("AA"):
            dict_aa[key] += int(pcs)
        else:
            dict_non_aa[key] += int(pcs)

    columns = ["Stone", "Cut", "Size", "PCS", "Grade"]

    data_aa = [
        {"Stone": key[0], "Cut": key[1], "Size": key[2], "PCS": value, "Grade": key[3]}
        for key, value in dict_aa.items()
    ]
    data_non_aa = [
        {"Stone": key[0], "Cut": key[1], "Size": key[2], "PCS": value, "Grade": key[3]}
        for key, value in dict_non_aa.items()
    ]

    df_aa = pd.DataFrame(data_aa, columns=columns)
    df_non_aa = pd.DataFrame(data_non_aa, columns=columns)

    if not df_aa.empty:
        df_aa = df_aa.sort_values(["Stone", "Cut", "Size"]).reset_index(drop=True)
    if not df_non_aa.empty:
        df_non_aa = df_non_aa.sort_values(["Stone", "Cut", "Size"]).reset_index(drop=True)

    return df_aa, df_non_aa


@st.cache_data(show_spinner=False)
def process_pdf(pdf_bytes: bytes, mapping_items):
    mapping_dict = dict(mapping_items)

    page_texts = extract_native_text(pdf_bytes)

    if native_text_is_usable(page_texts):
        pairs, unparsed = extract_pairs_from_text(page_texts, mapping_dict)
        stats = {
            "method": "Text",
            "pages": len(page_texts),
            "product_headings": len(pairs) + len(unparsed),
            "inventory_rows": len(pairs) + len(unparsed),
            "parsed_rows": len(pairs),
            "unparsed_rows": len(unparsed),
        }
    else:
        pairs, unparsed, stats = ocr_pdf(pdf_bytes, mapping_dict)
        stats["method"] = "OCR"

    df_aa, df_non_aa = build_dataframes(pairs)
    df_unparsed = pd.DataFrame(unparsed, columns=["Page", "Raw text", "PCS"])

    return df_aa, df_non_aa, df_unparsed, stats


def read_mapping_file(mapping_file):
    if mapping_file.name.lower().endswith(".csv"):
        file_bytes = mapping_file.getvalue()
        last_error = None
        for encoding in ["utf-8", "utf-8-sig", "cp874", "tis-620"]:
            try:
                return pd.read_csv(
                    io.BytesIO(file_bytes),
                    sep=None,
                    engine="python",
                    encoding=encoding,
                )
            except Exception as error:
                last_error = error
        raise ValueError(f"อ่าน CSV ไม่สำเร็จ: {last_error}")

    return pd.read_excel(mapping_file)


def create_excel(edited_df_aa, edited_df_non_aa, edited_df_unparsed, stats):
    output = io.BytesIO()

    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        has_sheet = False

        if not edited_df_aa.empty:
            edited_df_aa.to_excel(writer, sheet_name="AA_Grade", index=False)
            has_sheet = True

        if not edited_df_non_aa.empty:
            edited_df_non_aa.to_excel(writer, sheet_name="Non_AA_Grade", index=False)
            has_sheet = True

        if not edited_df_unparsed.empty:
            edited_df_unparsed.to_excel(writer, sheet_name="Needs_Review", index=False)
            has_sheet = True

        pd.DataFrame(
            [
                {"รายการ": "วิธีอ่านไฟล์", "ค่า": stats.get("method", "")},
                {"รายการ": "จำนวนหน้า", "ค่า": stats.get("pages", 0)},
                {"รายการ": "หัวข้อสินค้าที่ตรวจพบ", "ค่า": stats.get("product_headings", 0)},
                {"รายการ": "Total Inventory ที่ตรวจพบ", "ค่า": stats.get("inventory_rows", 0)},
                {"รายการ": "รายการที่แยกสำเร็จ", "ค่า": stats.get("parsed_rows", 0)},
                {"รายการ": "รายการที่ต้องตรวจทาน", "ค่า": stats.get("unparsed_rows", 0)},
            ]
        ).to_excel(writer, sheet_name="Summary", index=False)
        has_sheet = True

        if not has_sheet:
            pd.DataFrame({"Result": ["ไม่พบข้อมูล"]}).to_excel(
                writer, sheet_name="Summary", index=False
            )

    return output.getvalue()


# -----------------------------
# ส่วนติดต่อผู้ใช้
# -----------------------------
st.divider()
st.subheader("1. ฐานข้อมูลแปลงชื่อพลอย (ไม่บังคับ)")
st.caption("อัปโหลด Excel หรือ CSV โดยคอลัมน์แรกเป็นตัวย่อ และคอลัมน์ที่สองเป็นชื่อเต็ม")

mapping_file = st.file_uploader(
    "อัปโหลดไฟล์แปลงชื่อ",
    type=["xlsx", "xls", "csv"],
    key="mapping_file",
)

mapping_dict = {}
if mapping_file is not None:
    try:
        df_map = read_mapping_file(mapping_file)

        if df_map.shape[1] < 2:
            st.error("ไฟล์แปลงชื่อต้องมีอย่างน้อย 2 คอลัมน์")
        else:
            abbreviations = df_map.iloc[:, 0].astype(str).str.strip()
            full_names = df_map.iloc[:, 1].astype(str).str.strip()
            mapping_dict = dict(zip(abbreviations, full_names))

            # เพิ่ม key ตัวพิมพ์ใหญ่ เพื่อช่วยกรณี OCR
            mapping_dict.update(
                {
                    str(key).upper(): value
                    for key, value in list(mapping_dict.items())
                }
            )
            st.success(f"โหลดข้อมูลสำเร็จ: {len(df_map)} รายการ")
    except Exception as error:
        st.error(f"ไม่สามารถอ่านไฟล์แปลงชื่อได้: {error}")


st.divider()
st.subheader("2. อัปโหลดไฟล์ OSD Report (PDF)")
uploaded_file = st.file_uploader(
    "อัปโหลดไฟล์ PDF",
    type=["pdf"],
    key="osd_pdf",
)

if uploaded_file is not None:
    pdf_bytes = uploaded_file.getvalue()

    try:
        with st.spinner(
            "กำลังอ่าน PDF… หากไฟล์ใช้ฟอนต์พิเศษ ระบบจะใช้ OCR และอาจใช้เวลา 1–3 นาที"
        ):
            mapping_items = tuple(sorted(mapping_dict.items()))
            df_aa, df_non_aa, df_unparsed, stats = process_pdf(
                pdf_bytes,
                mapping_items,
            )

        if stats["method"] == "OCR":
            st.info(
                "ไฟล์นี้อ่านข้อความตรง ๆ ไม่ได้ ระบบจึงใช้ OCR อัตโนมัติ "
                "กรุณาตรวจทานชื่อ Stone, Cut และ Size ก่อนดาวน์โหลด"
            )
        else:
            st.success("อ่านข้อมูลจากข้อความใน PDF สำเร็จ")

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("จำนวนหน้า", stats.get("pages", 0))
        col2.metric("Total Inventory", stats.get("inventory_rows", 0))
        col3.metric("แยกสำเร็จ", stats.get("parsed_rows", 0))
        col4.metric("ต้องตรวจทาน", stats.get("unparsed_rows", 0))

        st.divider()

        edited_df_aa = pd.DataFrame()
        edited_df_non_aa = pd.DataFrame()
        edited_df_unparsed = pd.DataFrame()

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

        if not df_unparsed.empty:
            st.warning(
                "มีบางรายการที่ OCR แยกช่องไม่สำเร็จ กรุณาตรวจในตารางนี้ "
                "ข้อมูลจะถูกใส่ในชีต Needs_Review"
            )
            edited_df_unparsed = st.data_editor(
                df_unparsed,
                key="editor_unparsed",
                use_container_width=True,
                num_rows="dynamic",
            )

        has_any_result = (
            not edited_df_aa.empty
            or not edited_df_non_aa.empty
            or not edited_df_unparsed.empty
        )

        if has_any_result:
            excel_data = create_excel(
                edited_df_aa,
                edited_df_non_aa,
                edited_df_unparsed,
                stats,
            )

            st.divider()
            st.download_button(
                label="3. ดาวน์โหลดไฟล์ Excel",
                data=excel_data,
                file_name="OSD_Report_Updated_Names.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        else:
            st.error(
                "ยังไม่พบข้อมูลจาก PDF จึงยังไม่สร้างไฟล์ Excel "
                "กรุณาตรวจว่าเป็นรายงาน OSD Stone และลองอัปโหลดใหม่"
            )

    except pytesseract.TesseractNotFoundError:
        st.error(
            "ไม่พบ Tesseract OCR บนเซิร์ฟเวอร์ กรุณาตรวจ packages.txt "
            "ว่ามี tesseract-ocr และ tesseract-ocr-eng"
        )
    except Exception as error:
        st.exception(error)
