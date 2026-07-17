import io
import re
from collections import defaultdict

import pandas as pd
import pdfplumber
import pypdfium2 as pdfium
import pytesseract
import streamlit as st
from PIL import Image, ImageEnhance, ImageOps
from pytesseract import Output


st.set_page_config(
    page_title="OSD Stone Extractor",
    page_icon="💎",
    layout="wide",
)
st.title("💎 ระบบแยกข้อมูล OSD Stone")


# =========================================================
# ค่าที่ใช้กับ OCR
# =========================================================
LEFT_BOX = (0.03, 0.05, 0.49, 0.98)
RIGHT_BOX = (0.56, 0.04, 0.995, 0.98)

SEP_PATTERN = r"[/I|\\]"
SIZE_OCR_CHARS = "0-9OIlGSBMZE"

PRODUCT_PATTERN = re.compile(
    rf"^\s*"
    rf"(?P<stone>[A-Za-z][A-Za-z0-9()#_+\- ]{{0,24}}?)"
    rf"{SEP_PATTERN}"
    rf"(?P<cut>.+?)"
    rf"{SEP_PATTERN}"
    rf"(?P<size>[{SIZE_OCR_CHARS}][{SIZE_OCR_CHARS}.*xX+\-\'\"“”°‘’×]*)"
    rf"{SEP_PATTERN}"
    rf"(?P<grade>[A-Za-z0-9][A-Za-z0-9@()_.\-]*)",
    re.IGNORECASE,
)

SIZE_TRANSLATION = str.maketrans(
    {
        "O": "0",
        "o": "0",
        "I": "1",
        "l": "1",
        "G": "6",
        "S": "5",
        "B": "8",
        "M": "1",
        "Z": "2",
        "E": "6",
        "°": "*",
        "“": "*",
        "”": "*",
        '"': "*",
        "'": "*",
        "‘": "*",
        "’": "*",
        "×": "*",
    }
)


# =========================================================
# ฟังก์ชันทั่วไป
# =========================================================
def clean_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_grade(grade: str) -> str:
    grade = clean_spaces(grade).upper().strip("-_.")
    if grade in {"0A", "OA"}:
        return "AA"
    if grade == "8":
        return "B"
    if grade == "IAAA":
        return "AAA"
    return grade


def normalize_size(size: str) -> str:
    # M1.50 มักเกิดจากขีด / เกินหน้าตัวเลข 1
    size = re.sub(r"^M(?=1\.)", "", size)
    size = size.translate(SIZE_TRANSLATION)
    size = re.sub(r"\*+", "*", size)
    return size.strip("* .")


def lookup_stone_name(stone_abbr: str, mapping_dict: dict) -> str:
    return mapping_dict.get(
        stone_abbr,
        mapping_dict.get(stone_abbr.upper(), stone_abbr),
    )


def repair_product_ocr_text(line: str) -> str:
    """ซ่อมรูปแบบที่ Tesseract มักอ่านเครื่องหมาย / หรือเลข 1 ผิด"""
    line = clean_spaces(line)
    line = (
        line.replace("／", "/")
        .replace("｜", "/")
        .replace("\\", "/")
        .replace("|", "/")
    )

    # Stone ที่ลงท้ายวงเล็บ เช่น CT(M)/..., YEM(D)/...
    line = re.sub(
        r"^([A-Za-z0-9#_+\-]+\([A-Za-z]+\))(?=[A-Za-z])",
        r"\1/",
        line,
    )

    # กรณี OCR อ่าน )/ เป็น J หรือ Y เช่น ZTZ(MJOVH...
    line = re.sub(
        r"^([A-Za-z0-9#_+\-]+\([A-Za-z])(?:J|Y)(?=[A-Za-z])",
        r"\1)/",
        line,
    )

    # OCR อ่าน /1 เป็น M ก่อนตัวเลข เช่น FANCYM6*9 -> FANCY/16*9
    line = re.sub(r"(?<=[A-Za-z)])M(?=\d)", "/1", line)
    line = re.sub(r"(?<=[A-Za-z)])M(?=\.)", "/1", line)

    # Size ที่เลข 1 หาย เช่น /.00/ หรือ /.50/
    line = re.sub(r"/(?=\.(?:00|50)(?:/|I))", "/1", line)

    # OCR อ่าน /10*... เป็น O*... ต่อท้าย Cut
    line = re.sub(r"(?<=[A-Za-z)])O(?=[*'\"“”°])", "/10", line)

    # OCR อ่าน / หน้า Grade เป็นเลข 1 เช่น 4.4*21B -> 4.4*2/B
    line = re.sub(
        r"(?<=[0-9*.'\"“”°])1(?=(?:AA|AAA|A|B|G|CUSTOMER))",
        "/",
        line,
        flags=re.IGNORECASE,
    )

    # ถ้า Cut ต่อกับ Size ตรง ๆ เช่น OVH210/AA ให้แทรก /
    line = re.sub(
        r"(?<=[A-Za-z)])(?=[0-9Z][0-9OIlGSBMZ.*'\"“”°]*?(?:/|I)[A-Za-z])",
        "/",
        line,
    )

    return line


def parse_product_line(line: str, mapping_dict: dict):
    """แยกหัวข้อ Product เป็น Stone / Cut / Size / Grade"""
    line = repair_product_ocr_text(line)

    match = PRODUCT_PATTERN.search(line)
    if not match:
        return None

    stone_abbr = re.sub(r"\s+", "", match.group("stone")).strip("-_")
    cut = clean_spaces(match.group("cut")).strip(" /-_")
    size = normalize_size(match.group("size"))
    grade = normalize_grade(match.group("grade"))

    # OCR บางครั้งอ่านวงเล็บปิดของ Stone หาย
    if stone_abbr.count("(") > stone_abbr.count(")"):
        stone_abbr += ")"

    if not stone_abbr or not cut or not size or not grade:
        return None

    stone = lookup_stone_name(stone_abbr, mapping_dict)
    return stone, cut, size, grade


def parse_bl_value(total_line: str):
    """อ่านค่า B/L ซึ่งเป็นเลขตัวสุดท้ายของบรรทัด Total Inventory"""
    line = clean_spaces(total_line).replace("−", "-").replace("–", "-")

    # รองรับข้อความเช่น 14-3 หรือ = -3
    match = re.search(r"(-\s*\d+|\d+)\s*$", line)
    if not match:
        return None

    value = match.group(1).replace(" ", "")
    try:
        return int(value)
    except ValueError:
        return None


def prepare_image(image: Image.Image, contrast: float = 1.35) -> Image.Image:
    image = image.convert("L")
    image = ImageOps.autocontrast(image)
    image = ImageEnhance.Contrast(image).enhance(contrast)
    return image


def crop_relative(image: Image.Image, box):
    width, height = image.size
    left, top, right, bottom = box
    return image.crop(
        (
            int(width * left),
            int(height * top),
            int(width * right),
            int(height * bottom),
        )
    )


def ocr_lines_with_position(
    image: Image.Image,
    box,
    psm: int,
    upscale: float = 1.2,
):
    """OCR แล้วคืนข้อความพร้อมตำแหน่ง Y แบบสัดส่วนของหน้า"""
    page_width, page_height = image.size
    left, top, right, bottom = box

    crop = crop_relative(image, box)
    crop = crop.resize(
        (int(crop.width * upscale), int(crop.height * upscale))
    )
    crop = prepare_image(crop)

    data = pytesseract.image_to_data(
        crop,
        lang="eng",
        config=f"--oem 3 --psm {psm}",
        output_type=Output.DICT,
    )

    groups = {}
    for index, text in enumerate(data["text"]):
        text = str(text).strip()
        if not text:
            continue

        try:
            confidence = float(data["conf"][index])
        except (TypeError, ValueError):
            confidence = -1

        if confidence < 0:
            continue

        key = (
            data["block_num"][index],
            data["par_num"][index],
            data["line_num"][index],
        )
        group = groups.setdefault(
            key,
            {"words": [], "top": 10**9, "bottom": 0},
        )
        group["words"].append(text)
        group["top"] = min(group["top"], data["top"][index])
        group["bottom"] = max(
            group["bottom"],
            data["top"][index] + data["height"][index],
        )

    lines = []
    for group in groups.values():
        center_y_in_crop = (group["top"] + group["bottom"]) / 2
        page_y = top + center_y_in_crop / (upscale * page_height)
        lines.append((page_y, clean_spaces(" ".join(group["words"]))))

    return sorted(lines, key=lambda item: item[0])


def dedupe_y_positions(positions, tolerance: float = 0.006):
    result = []
    for value in sorted(positions):
        if not result or abs(value - result[-1]) > tolerance:
            result.append(value)
    return result


def looks_like_product_heading(line: str) -> bool:
    upper = clean_spaces(line).upper()

    if not upper or not re.match(r"^[A-Z]", upper):
        return False
    if not re.search(r"\d", upper):
        return False
    if re.search(r"\d{1,2}/\d{1,2}/20\d{2}", upper):
        return False
    if any(
        blocked in upper
        for blocked in (
            "BATCH PRODUCT",
            "TOTAL INVENTORY",
            "USD STL",
            "HTTP",
            "REPORT",
            "IQNIOTE",
            "NET/LIVE",
            "FACTORY",
            "JEWELLERS",
        )
    ):
        return False

    # หัวข้อ Product ต้องมีตัวแบ่งหลายตำแหน่ง และมี Grade อยู่ช่วงท้าย
    separator_count = sum(upper.count(char) for char in ("/", "I", "|", "\\"))
    grade_hint = re.search(
        r"(?:/|I)(?:A{1,3}|[A-Z]|CUSTOMER(?:\([A-Z0-9]+\))?)(?:@[A-Z0-9_()\-]+)?",
        upper,
    )

    return separator_count >= 2 and grade_hint is not None and len(upper) <= 140


def find_heading_y_positions(page_image: Image.Image):
    coarse_lines = ocr_lines_with_position(
        page_image,
        LEFT_BOX,
        psm=4,
        upscale=1.2,
    )

    positions = [
        y for y, text in coarse_lines if looks_like_product_heading(text)
    ]
    return dedupe_y_positions(positions)


def find_total_y_positions(page_image: Image.Image):
    coarse_lines = ocr_lines_with_position(
        page_image,
        RIGHT_BOX,
        psm=11,
        upscale=1.2,
    )

    positions = []
    for y, text in coarse_lines:
        if re.search(r"INVENT|NVENTORY|VENTOR", text, flags=re.IGNORECASE):
            positions.append(y)

    return dedupe_y_positions(positions)


def build_line_strip(
    page_image: Image.Image,
    page_y: float,
    x_left: float,
    x_right: float,
    half_height_top: float,
    half_height_bottom: float,
    upscale: float,
):
    width, height = page_image.size
    strip = page_image.crop(
        (
            int(width * x_left),
            int(height * (page_y - half_height_top)),
            int(width * x_right),
            int(height * (page_y + half_height_bottom)),
        )
    )
    strip = strip.resize(
        (int(strip.width * upscale), int(strip.height * upscale))
    )
    return prepare_image(strip, contrast=1.5)


def ocr_strip_canvas(strips, psm: int = 6):
    """รวมหลายบรรทัดเป็นภาพเดียว เพื่อลดจำนวนครั้งที่เรียก Tesseract"""
    if not strips:
        return {}

    gap = 28
    canvas_width = max(strip.width for strip in strips)
    strip_height = max(strip.height for strip in strips)
    block_height = strip_height + gap
    canvas_height = block_height * len(strips)

    canvas = Image.new("L", (canvas_width, canvas_height), 255)
    for index, strip in enumerate(strips):
        if strip.size != (canvas_width, strip_height):
            strip = strip.resize((canvas_width, strip_height))
        canvas.paste(strip, (0, index * block_height))

    data = pytesseract.image_to_data(
        canvas,
        lang="eng",
        config=f"--oem 3 --psm {psm}",
        output_type=Output.DICT,
    )

    groups = {}
    for index, text in enumerate(data["text"]):
        text = str(text).strip()
        if not text:
            continue

        try:
            confidence = float(data["conf"][index])
        except (TypeError, ValueError):
            confidence = -1

        if confidence < 0:
            continue

        line_key = (
            data["block_num"][index],
            data["par_num"][index],
            data["line_num"][index],
        )
        group = groups.setdefault(
            line_key,
            {"words": [], "top": 10**9, "bottom": 0},
        )
        group["words"].append(text)
        group["top"] = min(group["top"], data["top"][index])
        group["bottom"] = max(
            group["bottom"],
            data["top"][index] + data["height"][index],
        )

    block_lines = defaultdict(list)
    for group in groups.values():
        center_y = (group["top"] + group["bottom"]) / 2
        block_index = int(center_y // block_height)
        if 0 <= block_index < len(strips):
            block_lines[block_index].append(
                clean_spaces(" ".join(group["words"]))
            )

    return block_lines


def ocr_product_strips(page_image, y_positions, mapping_dict):
    """อ่านหัวข้อ Product และคงตำแหน่ง Y ของแต่ละรายการ"""
    strips = [
        build_line_strip(
            page_image,
            y,
            x_left=0.035,
            x_right=0.50,
            half_height_top=0.010,
            half_height_bottom=0.012,
            upscale=2.5,
        )
        for y in y_positions
    ]

    block_lines = ocr_strip_canvas(strips, psm=6)
    results = []

    for index, y in enumerate(y_positions):
        selected_product = None
        selected_text = ""

        for text in block_lines.get(index, []):
            product = parse_product_line(text, mapping_dict)
            if product is not None:
                selected_product = product
                selected_text = text
                break

        # fallback เฉพาะบรรทัดที่ภาพรวมอ่านไม่สำเร็จ
        if selected_product is None and index < len(strips):
            selected_text = clean_spaces(
                pytesseract.image_to_string(
                    strips[index],
                    lang="eng",
                    config="--oem 3 --psm 7",
                )
            )
            selected_product = parse_product_line(
                selected_text,
                mapping_dict,
            )

        results.append((y, selected_product, selected_text))

    return results


def ocr_total_strips(page_image, y_positions):
    strips = [
        build_line_strip(
            page_image,
            y,
            x_left=0.56,
            x_right=0.995,
            half_height_top=0.009,
            half_height_bottom=0.011,
            upscale=2.3,
        )
        for y in y_positions
    ]

    block_lines = ocr_strip_canvas(strips, psm=6)
    results = []

    for index, y in enumerate(y_positions):
        selected_text = ""
        bl_value = None

        candidate_lines = block_lines.get(index, [])
        inventory_lines = [
            text
            for text in candidate_lines
            if re.search(r"INVENT|NVENTORY|VENTOR|TOTAL", text, re.IGNORECASE)
        ]

        for text in inventory_lines + candidate_lines:
            value = parse_bl_value(text)
            if value is not None:
                selected_text = text
                bl_value = value
                break

        if bl_value is None and index < len(strips):
            selected_text = clean_spaces(
                pytesseract.image_to_string(
                    strips[index],
                    lang="eng",
                    config="--oem 3 --psm 7",
                )
            )
            bl_value = parse_bl_value(selected_text)

        results.append((y, bl_value, selected_text))

    return results


# =========================================================
# อ่าน PDF แบบข้อความปกติ
# =========================================================
def native_pdf_is_usable(page_texts) -> bool:
    joined = "\n".join(page_texts)
    has_inventory = re.search(r"TOTAL\s+INVENTORY", joined, re.IGNORECASE)
    has_product = re.search(
        r"[A-Za-z][^\n]*/[^\n]*/[0-9][^\n]*/[A-Za-z]",
        joined,
    )
    return bool(has_inventory and has_product)


def extract_native_events(pdf_bytes: bytes, mapping_dict: dict):
    product_pattern = re.compile(
        r"([A-Za-z][A-Za-z0-9()#_+\-]*)\s*/\s*"
        r"(.+?)\s*/\s*"
        r"([0-9.*xX+\-]+)\s*/\s*"
        r"([A-Za-z0-9()@\s_.+\-]+)"
    )

    events = []
    page_texts = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            page_texts.append(text)

            for line_number, raw_line in enumerate(text.splitlines()):
                line = clean_spaces(raw_line)

                product_match = product_pattern.search(line)
                if product_match:
                    stone_abbr = product_match.group(1).strip()
                    cut = product_match.group(2).strip()
                    size = product_match.group(3).strip()
                    grade = product_match.group(4).strip()
                    grade = re.split(r"\s{2,}", grade)[0]
                    grade = re.sub(r"\s+[\d.]+$", "", grade).strip()
                    stone = lookup_stone_name(stone_abbr, mapping_dict)
                    events.append(
                        (
                            page_number,
                            line_number,
                            "product",
                            (stone, cut, size, grade),
                            line,
                        )
                    )

                if re.search(r"TOTAL\s+INVENTORY", line, re.IGNORECASE):
                    events.append(
                        (
                            page_number,
                            line_number + 0.5,
                            "total",
                            parse_bl_value(line),
                            line,
                        )
                    )

    return page_texts, events


# =========================================================
# อ่าน PDF แบบ OCR ตามตำแหน่งจริงของรายงาน
# =========================================================
def product_needs_refinement(raw_text: str, product) -> bool:
    if product is None:
        return True

    _, cut, size, grade = product
    upper_raw = raw_text.upper()

    return any(
        (
            cut.startswith("/"),
            cut.endswith("I"),
            grade.endswith("."),
            grade.isdigit(),
            size.startswith("0"),
            "“" in raw_text,
            "”" in raw_text,
            "°" in raw_text,
            "‘" in raw_text,
            bool(re.search(r"[A-Z]I[0-9OIlGSBMZ]", upper_raw)),
            bool(re.search(r"[0-9OIlGSBMZ]I[A-Z]", upper_raw)),
        )
    )


def refine_product_from_line(page_image, y, mapping_dict):
    strip = build_line_strip(
        page_image,
        y,
        x_left=0.035,
        x_right=0.50,
        half_height_top=0.011,
        half_height_bottom=0.013,
        upscale=2.8,
    )
    text = clean_spaces(
        pytesseract.image_to_string(
            strip,
            lang="eng",
            config="--oem 3 --psm 7",
        )
    )
    return parse_product_line(text, mapping_dict), text


def extract_ocr_events(pdf_bytes: bytes, mapping_dict: dict):
    """OCR แบบเร็ว: อ่านฝั่ง Product และ Total Inventory อย่างละหนึ่งครั้งต่อหน้า"""
    document = pdfium.PdfDocument(pdf_bytes)
    events = []
    debug_unparsed = []

    for page_index in range(len(document)):
        page_number = page_index + 1
        page_image = document[page_index].render(scale=2.2).to_pil().convert("RGB")

        product_lines = ocr_lines_with_position(
            page_image,
            LEFT_BOX,
            psm=4,
            upscale=1.2,
        )
        total_lines = ocr_lines_with_position(
            page_image,
            RIGHT_BOX,
            psm=6,
            upscale=1.2,
        )

        product_events_on_page = []
        for y, raw_text in product_lines:
            product = parse_product_line(raw_text, mapping_dict)

            is_product_candidate = product is not None or looks_like_product_heading(raw_text)
            if not is_product_candidate:
                continue

            if product is not None:
                product_events_on_page.append(
                    (page_number, y, "product", product, raw_text)
                )
            else:
                debug_unparsed.append(
                    {
                        "Page": page_number,
                        "Type": "Product",
                        "Raw text": raw_text,
                    }
                )

        # ป้องกัน Product ซ้ำที่ OCR แบ่งเป็นหลายกลุ่มในตำแหน่งเดียวกัน
        last_y = None
        for event in sorted(product_events_on_page, key=lambda item: item[1]):
            if last_y is None or abs(event[1] - last_y) > 0.006:
                events.append(event)
                last_y = event[1]

        for y, raw_text in total_lines:
            if not re.search(
                r"INVENT|NVENTORY|VENTOR",
                raw_text,
                flags=re.IGNORECASE,
            ):
                continue

            bl_value = parse_bl_value(raw_text)
            events.append(
                (page_number, y, "total", bl_value, raw_text)
            )

            if bl_value is None:
                debug_unparsed.append(
                    {
                        "Page": page_number,
                        "Type": "Total Inventory",
                        "Raw text": raw_text,
                    }
                )

    return events, debug_unparsed, len(document)


# =========================================================
# รวมผลลัพธ์และสร้างตาราง
# =========================================================
def events_to_dataframes(events):
    events = sorted(events, key=lambda item: (item[0], item[1]))

    dict_aa = defaultdict(int)
    dict_non_aa = defaultdict(int)
    current_product = None

    detected_products = 0
    detected_totals = 0
    shortage_rows = 0

    for _, _, event_type, value, _ in events:
        if event_type == "product":
            current_product = value
            detected_products += 1
            continue

        detected_totals += 1

        # ทุก Total Inventory ปิด Product ปัจจุบัน ไม่ว่า B/L บวกหรือลบ
        if current_product is not None and value is not None and value < 0:
            stone, cut, size, grade = current_product
            pcs = abs(value)
            key = (stone, cut, size, grade)

            if grade.upper().startswith("AA"):
                dict_aa[key] += pcs
            else:
                dict_non_aa[key] += pcs

            shortage_rows += 1

        current_product = None

    columns = ["Stone", "Cut", "Size", "PCS", "Grade"]

    data_aa = [
        {
            "Stone": key[0],
            "Cut": key[1],
            "Size": key[2],
            "PCS": value,
            "Grade": key[3],
        }
        for key, value in dict_aa.items()
    ]
    data_non_aa = [
        {
            "Stone": key[0],
            "Cut": key[1],
            "Size": key[2],
            "PCS": value,
            "Grade": key[3],
        }
        for key, value in dict_non_aa.items()
    ]

    df_aa = pd.DataFrame(data_aa, columns=columns)
    df_non_aa = pd.DataFrame(data_non_aa, columns=columns)

    if not df_aa.empty:
        df_aa = df_aa.sort_values(
            by=["Stone", "Cut", "Size"],
            key=lambda column: column.astype(str),
        ).reset_index(drop=True)

    if not df_non_aa.empty:
        df_non_aa = df_non_aa.sort_values(
            by=["Stone", "Cut", "Size"],
            key=lambda column: column.astype(str),
        ).reset_index(drop=True)

    stats = {
        "detected_products": detected_products,
        "detected_totals": detected_totals,
        "shortage_rows": shortage_rows,
        "output_rows": len(df_aa) + len(df_non_aa),
    }
    return df_aa, df_non_aa, stats


@st.cache_data(show_spinner=False)
def process_pdf(pdf_bytes: bytes, mapping_items):
    mapping_dict = dict(mapping_items)

    page_texts, native_events = extract_native_events(pdf_bytes, mapping_dict)

    if native_pdf_is_usable(page_texts):
        events = native_events
        debug_unparsed = []
        page_count = len(page_texts)
        method = "Text"
    else:
        events, debug_unparsed, page_count = extract_ocr_events(
            pdf_bytes,
            mapping_dict,
        )
        method = "OCR"

    df_aa, df_non_aa, stats = events_to_dataframes(events)
    stats["method"] = method
    stats["pages"] = page_count

    debug_df = pd.DataFrame(
        debug_unparsed,
        columns=["Page", "Type", "Raw text"],
    )
    return df_aa, df_non_aa, debug_df, stats


# =========================================================
# อ่านไฟล์ Mapping และสร้าง Excel
# =========================================================
def read_mapping_file(mapping_file):
    if mapping_file.name.lower().endswith(".csv"):
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

        raise ValueError(f"อ่าน CSV ไม่สำเร็จ: {last_error}")

    return pd.read_excel(mapping_file)


def create_excel(df_aa, df_non_aa):
    output = io.BytesIO()

    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        workbook = writer.book
        header_format = workbook.add_format(
            {
                "bold": True,
                "align": "center",
                "valign": "vcenter",
                "border": 1,
            }
        )
        body_center = workbook.add_format(
            {"align": "center", "valign": "vcenter"}
        )
        text_format = workbook.add_format({"num_format": "@"})

        sheets_written = 0

        for sheet_name, dataframe in (
            ("AA_Grade", df_aa),
            ("Non_AA_Grade", df_non_aa),
        ):
            if dataframe.empty:
                continue

            dataframe.to_excel(writer, sheet_name=sheet_name, index=False)
            worksheet = writer.sheets[sheet_name]
            worksheet.freeze_panes(1, 0)
            worksheet.autofilter(0, 0, len(dataframe), len(dataframe.columns) - 1)
            worksheet.set_row(0, 22, header_format)
            worksheet.set_column("A:A", 16)
            worksheet.set_column("B:B", 30)
            worksheet.set_column("C:C", 14, text_format)
            worksheet.set_column("D:D", 10, body_center)
            worksheet.set_column("E:E", 18)
            sheets_written += 1

        if sheets_written == 0:
            pd.DataFrame({"Result": ["ไม่พบรายการ B/L ติดลบ"]}).to_excel(
                writer,
                sheet_name="Result",
                index=False,
            )

    return output.getvalue()


# =========================================================
# ส่วนติดต่อผู้ใช้
# =========================================================
st.divider()
st.subheader("1. ฐานข้อมูลแปลงชื่อพลอย (ไม่บังคับ)")
st.caption(
    "อัปโหลด Excel หรือ CSV โดยคอลัมน์แรกเป็นตัวย่อ และคอลัมน์ที่สองเป็นชื่อเต็ม"
)

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
            mapping_dict.update(
                {
                    str(key).upper(): value
                    for key, value in list(mapping_dict.items())
                }
            )
            st.success(f"โหลดข้อมูลสำเร็จ: พบรายชื่อพลอย {len(df_map)} รายการ")
    except Exception as error:
        st.error(f"ไม่สามารถอ่านไฟล์แปลงชื่อได้: {error}")


st.divider()
st.subheader("2. อัปโหลดไฟล์ OSD Report (PDF)")
uploaded_file = st.file_uploader(
    "อัปโหลดไฟล์ PDF ตรงนี้",
    type="pdf",
    key="osd_pdf",
)

if uploaded_file is not None:
    pdf_bytes = uploaded_file.getvalue()

    try:
        with st.spinner(
            "กำลังแยก Stone / Cut / Size / PCS / Grade… "
            "ไฟล์ที่ใช้ฟอนต์พิเศษอาจใช้เวลา 2–5 นาที"
        ):
            mapping_items = tuple(sorted(mapping_dict.items()))
            df_aa, df_non_aa, debug_df, stats = process_pdf(
                pdf_bytes,
                mapping_items,
            )

        if stats["method"] == "OCR":
            st.info(
                "PDF นี้ดึงข้อความตรง ๆ ไม่ได้ ระบบจึงอ่านตามตำแหน่งในรายงานด้วย OCR"
            )
        else:
            st.success("อ่านข้อมูลจากข้อความใน PDF สำเร็จ")

        metric1, metric2, metric3, metric4 = st.columns(4)
        metric1.metric("จำนวนหน้า", stats["pages"])
        metric2.metric("Product ที่พบ", stats["detected_products"])
        metric3.metric("B/L ติดลบ", stats["shortage_rows"])
        metric4.metric("รายการหลังรวมซ้ำ", stats["output_rows"])

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
                hide_index=True,
                column_config={
                    "Stone": st.column_config.TextColumn("Stone"),
                    "Cut": st.column_config.TextColumn("Cut"),
                    "Size": st.column_config.TextColumn("Size"),
                    "PCS": st.column_config.NumberColumn("PCS", min_value=0, step=1),
                    "Grade": st.column_config.TextColumn("Grade"),
                },
            )

        if not df_non_aa.empty:
            st.write(f"**พลอยเกรดอื่น ๆ ({len(df_non_aa)} รายการ)**")
            edited_df_non_aa = st.data_editor(
                df_non_aa,
                key="editor_non_aa",
                use_container_width=True,
                num_rows="dynamic",
                hide_index=True,
                column_config={
                    "Stone": st.column_config.TextColumn("Stone"),
                    "Cut": st.column_config.TextColumn("Cut"),
                    "Size": st.column_config.TextColumn("Size"),
                    "PCS": st.column_config.NumberColumn("PCS", min_value=0, step=1),
                    "Grade": st.column_config.TextColumn("Grade"),
                },
            )

        if df_aa.empty and df_non_aa.empty:
            st.error("ไม่พบรายการ B/L ติดลบจากไฟล์นี้")
        else:
            excel_data = create_excel(edited_df_aa, edited_df_non_aa)
            st.divider()
            st.download_button(
                label="3. ดาวน์โหลดไฟล์ Excel",
                data=excel_data,
                file_name="OSD_Report_Updated_Names.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

        if not debug_df.empty:
            st.warning(
                f"มีข้อความ OCR ที่ควรตรวจทาน {len(debug_df)} บรรทัด "
                "แต่ไม่ได้ใส่ Raw text ลงในไฟล์ Excel"
            )
            with st.expander("ดูข้อความที่ OCR อ่านไม่ครบ"):
                st.dataframe(debug_df, use_container_width=True, hide_index=True)

    except pytesseract.TesseractNotFoundError:
        st.error(
            "ไม่พบ Tesseract OCR กรุณาตรวจ packages.txt ให้มี "
            "tesseract-ocr และ tesseract-ocr-eng"
        )
    except Exception as error:
        st.exception(error)
  





