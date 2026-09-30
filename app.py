import io
import re
import json
import math
import zipfile
import hashlib
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


if "raw_upload_version" not in st.session_state:
    st.session_state.raw_upload_version = 0

if "pasted_raw_text" not in st.session_state:
    st.session_state.pasted_raw_text = ""


def clear_raw_inputs():
    """ล้างไฟล์ดิบ ข้อความ และตารางที่แก้ไขไว้บนหน้าจอ"""
    st.session_state.raw_upload_version += 1
    st.session_state.pasted_raw_text = ""
    st.session_state.pop("editor_aa", None)
    st.session_state.pop("editor_non_aa", None)
    for key in list(st.session_state):
        if key.startswith("supplier_selection_"):
            st.session_state.pop(key, None)
    st.cache_data.clear()


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


def calculate_column_width(
    dataframe: pd.DataFrame,
    column_name: str,
    minimum: int,
    maximum: int,
) -> int:
    """คำนวณความกว้างคอลัมน์จากข้อความจริง โดยจำกัดไม่ให้กว้างเกินไป"""
    values = [column_name]
    if column_name in dataframe.columns:
        values.extend(
            dataframe[column_name]
            .fillna("")
            .astype(str)
            .tolist()
        )

    longest = max((len(value) for value in values), default=minimum)
    return min(max(longest + 3, minimum), maximum)


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
                "bg_color": "#D9EAF7",
            }
        )
        text_center_format = workbook.add_format(
            {
                "num_format": "@",
                "border": 1,
                "align": "center",
                "valign": "vcenter",
            }
        )
        number_center_format = workbook.add_format(
            {
                "num_format": "0",
                "border": 1,
                "align": "center",
                "valign": "vcenter",
            }
        )

        for sheet_name, dataframe in (
            ("AA_Grade", df_aa),
            ("Non_AA_Grade", df_non_aa),
        ):
            dataframe.to_excel(writer, sheet_name=sheet_name, index=False)
            worksheet = writer.sheets[sheet_name]

            worksheet.freeze_panes(1, 0)
            worksheet.autofilter(
                0,
                0,
                len(dataframe),
                len(dataframe.columns) - 1,
            )
            worksheet.set_row(0, 24, header_format)
            worksheet.set_default_row(21)

            widths = {
                "Stone": calculate_column_width(dataframe, "Stone", 10, 24),
                "Cut": calculate_column_width(dataframe, "Cut", 10, 40),
                "Size": calculate_column_width(dataframe, "Size", 10, 24),
                "PCS": calculate_column_width(dataframe, "PCS", 8, 12),
                "Grade": calculate_column_width(dataframe, "Grade", 12, 30),
            }

            worksheet.set_column("A:A", widths["Stone"], text_center_format)
            worksheet.set_column("B:B", widths["Cut"], text_center_format)
            worksheet.set_column("C:C", widths["Size"], text_center_format)
            worksheet.set_column("D:D", widths["PCS"], number_center_format)
            worksheet.set_column("E:E", widths["Grade"], text_center_format)

    return output.getvalue()


SUPPLIER_RULES = json.loads('{"groups": {"01_Colour_Stones": ["AM", "AM(D)", "CT(L)", "CT(M)", "GN", "GN(P)", "GRAM", "LBT", "LSBT", "PAM", "PD", "RHGN", "SBT", "SWBT", "WT", "WZC"], "02_AQ_GRT_IO_MON_PT_TZ": ["AQ", "AX", "GRT", "GRT(L)", "IO", "MON", "PMON", "PT", "PT(L)", "TZ"], "03_PTSA_TBSA_AU": ["PTSA-AU", "TBSA-AU"], "04_BKSA": ["BKSA"], "05_TS": ["TS", "TS(D)", "TS(L)"], "06_Customer": ["AM", "GFP", "PFP", "SA", "WFP"], "07_EM": ["EM"], "08_Synthetic_CZ": ["YAQ#106", "YBLS#34", "YEM", "YEM(1101)", "YEMN", "YEMN(D)", "YEMN(L)", "YOP", "YPB", "YPPSAN", "YPPSAN(D)", "YPPSAN(L)", "YPS#2", "YPSAN", "YPSAN(D)", "YPSAN(L)", "YRU#8", "YRUN", "YRUN(D)", "YRUN(L)", "YSAN", "YSAN(D)", "YSAN(L)", "YWCI(1000)", "YWSA", "YYESAN", "YYESAN(D)", "YYESAN(L)", "ZBK", "ZC(M)", "ZGN(M)", "ZPD(M)", "ZPP(M)", "ZTZ(M)", "ZW"], "09_Special_Cut": ["AQ(DYE)", "AVYE", "BKA", "BKRT", "CN", "JD", "JDM", "JP", "LAVA", "LP", "LPM", "LRM", "MA", "MLC", "MOI", "OP", "OX", "RBMN", "RDN", "ROQ", "TGE", "TLOP-AU", "TQ", "TQ-AU", "TQM", "WFP", "WFP(N)", "WMOP", "WOP-AU", "YEM"], "10_Pearl": ["WFP"], "11_Ruby_Sapphire": ["CSA", "GRSA", "GRSA-AU", "OSA", "PSA", "PSA(300)", "PSA(500)", "PSA(D)", "PSA(L)", "RU", "RU(100)", "RU(D)", "RU(L)", "SA", "SA(L)", "SA-AU", "SAYE", "SAYE(L)"]}, "exact": {"AM|OC|AA": "01_Colour_Stones", "AM|PR2BR|AA": "01_Colour_Stones", "AM|RD|AA": "01_Colour_Stones", "AM|RD2FC|AA": "01_Colour_Stones", "AM|SQ|AA": "01_Colour_Stones", "AM(D)|RD|AA": "01_Colour_Stones", "CT(L)|RD|AA": "01_Colour_Stones", "CT(M)|BG|AA": "01_Colour_Stones", "CT(M)|MQ|AA": "01_Colour_Stones", "CT(M)|OC|AA": "01_Colour_Stones", "CT(M)|OV|AA": "01_Colour_Stones", "CT(M)|OVBRF|AA": "01_Colour_Stones", "CT(M)|PC|AA": "01_Colour_Stones", "CT(M)|RD|AA": "01_Colour_Stones", "GN|BG|AA": "01_Colour_Stones", "GN|OV2BR|AA": "01_Colour_Stones", "GN|PR2BR|AA": "01_Colour_Stones", "GN|PRBR|AA": "01_Colour_Stones", "GN|RD|AA": "01_Colour_Stones", "GN|RD2FC|AA": "01_Colour_Stones", "GN(P)|SQ|AA": "01_Colour_Stones", "GRAM|KT(SPELLBOUND)|AA": "01_Colour_Stones", "GRAM|OCH|AA": "01_Colour_Stones", "LBT|BG|AA": "01_Colour_Stones", "LBT|CUCB|AA": "01_Colour_Stones", "LBT|OC|AA": "01_Colour_Stones", "LBT|OV(SPECIAL CUT)|AA": "01_Colour_Stones", "LBT|OV2BR|AA": "01_Colour_Stones", "LBT|PR|AA": "01_Colour_Stones", "LBT|RD|AA": "01_Colour_Stones", "LBT|RD2BR|AA": "01_Colour_Stones", "LSBT|BG|AA": "01_Colour_Stones", "LSBT|PR|AA": "01_Colour_Stones", "LSBT|RD|AA": "01_Colour_Stones", "LSBT|RD2FC|AA": "01_Colour_Stones", "LSBT|SQ|AA": "01_Colour_Stones", "PAM|OC|AA": "01_Colour_Stones", "PAM|PR|AA": "01_Colour_Stones", "PAM|RD|AA": "01_Colour_Stones", "PD|BG|AA": "01_Colour_Stones", "PD|OVBRF|AA": "01_Colour_Stones", "PD|PR|AA": "01_Colour_Stones", "PD|RD|AA": "01_Colour_Stones", "PD|RD2BR|AA": "01_Colour_Stones", "PD|RD2FC|AA": "01_Colour_Stones", "PD|SQ|AA": "01_Colour_Stones", "RHGN|CU|AA": "01_Colour_Stones", "RHGN|MQ|AA": "01_Colour_Stones", "RHGN|OC|AA": "01_Colour_Stones", "RHGN|OV|AA": "01_Colour_Stones", "RHGN|OVBRF|AA": "01_Colour_Stones", "RHGN|PR|AA": "01_Colour_Stones", "RHGN|PRCB|AA": "01_Colour_Stones", "RHGN|RD|AA": "01_Colour_Stones", "RHGN|RD2FC|AA": "01_Colour_Stones", "RHGN|RDCB|AA": "01_Colour_Stones", "RHGN|SQ|AA": "01_Colour_Stones", "SBT|BG|AA": "01_Colour_Stones", "SBT|OV2BR|AA": "01_Colour_Stones", "SBT|PR2BR|AA": "01_Colour_Stones", "SBT|RD|AA": "01_Colour_Stones", "SBT|RD2BR|AA": "01_Colour_Stones", "SWBT|CU|AA": "01_Colour_Stones", "SWBT|PR|AA": "01_Colour_Stones", "SWBT|RD|AA": "01_Colour_Stones", "WT|HT|AA": "01_Colour_Stones", "WT|RD|AA": "01_Colour_Stones", "WZC|RD|AA": "01_Colour_Stones", "AM|OCRAD|A": "01_Colour_Stones", "AM|OV(SPECIAL CUT)|A": "01_Colour_Stones", "AM|PR2BR|A": "01_Colour_Stones", "AM|RD|A": "01_Colour_Stones", "LSBT|PR2BR|A": "01_Colour_Stones", "AQ|BG|AA": "02_AQ_GRT_IO_MON_PT_TZ", "AX|RD|AA": "02_AQ_GRT_IO_MON_PT_TZ", "GRT|PR|AA": "02_AQ_GRT_IO_MON_PT_TZ", "GRT(L)|RD|AA": "02_AQ_GRT_IO_MON_PT_TZ", "IO|TRCB|AA": "02_AQ_GRT_IO_MON_PT_TZ", "MON|BG|AA": "02_AQ_GRT_IO_MON_PT_TZ", "PMON|PR|AA": "02_AQ_GRT_IO_MON_PT_TZ", "PT|BG|AA": "02_AQ_GRT_IO_MON_PT_TZ", "PT|OC|AA": "02_AQ_GRT_IO_MON_PT_TZ", "PT|PR|AA": "02_AQ_GRT_IO_MON_PT_TZ", "PT|RD|AA": "02_AQ_GRT_IO_MON_PT_TZ", "PT(L)|RD|AA": "02_AQ_GRT_IO_MON_PT_TZ", "TZ|BG|AA": "02_AQ_GRT_IO_MON_PT_TZ", "TZ|RD|AA": "02_AQ_GRT_IO_MON_PT_TZ", "PT|RD|B": "02_AQ_GRT_IO_MON_PT_TZ", "PTSA-AU|MQ|AA": "03_PTSA_TBSA_AU", "PTSA-AU|RD|AA": "03_PTSA_TBSA_AU", "TBSA-AU|CU|AA": "03_PTSA_TBSA_AU", "BKSA|RD|B": "04_BKSA", "BKSA|TR|B": "04_BKSA", "TS|BG|AA": "05_TS", "TS|RD|AA": "05_TS", "TS(D)|BG|AA": "05_TS", "TS(D)|OC|AA@(RSK)": "05_TS", "TS(D)|RD|AA@(RSK)": "05_TS", "TS(D)|RD|AA": "05_TS", "TS(L)|RD|AA": "05_TS", "AM|MQ|Customer(Simon)": "06_Customer", "GFP|RD|Customer(MADS)": "06_Customer", "PFP|RD|Customer(MADS)": "06_Customer", "SA|CU|Customer(MADS)": "06_Customer", "SA|OV|Customer(MADS)": "06_Customer", "SA|RD|Customer(MADS)": "06_Customer", "SA|RD|Customer(Simon)": "06_Customer", "SA|SQ|Customer(MJS)": "06_Customer", "WFP|RD|Customer(MADS)": "06_Customer", "EM|MQ|AA": "07_EM", "EM|RD|AA": "07_EM", "EM|MQ|A": "07_EM", "EM|RD|A": "07_EM", "EM|RD|B": "07_EM", "YAQ#106|MQ|AA": "08_Synthetic_CZ", "YBLS#34|PR2BR|AA": "08_Synthetic_CZ", "YBLS#34|RD|AA": "08_Synthetic_CZ", "YEM|OV|AA": "08_Synthetic_CZ", "YEM|OV2BR|AA": "08_Synthetic_CZ", "YEM|PR2BR|AA": "08_Synthetic_CZ", "YEM|RD|AA": "08_Synthetic_CZ", "YEM(1101)|RD|AA": "08_Synthetic_CZ", "YEMN|RD|AA": "08_Synthetic_CZ", "YEMN(D)|RD|AA": "08_Synthetic_CZ", "YEMN(L)|RD|AA": "08_Synthetic_CZ", "YOP|CU|AA": "08_Synthetic_CZ", "YOP|MQ|AA": "08_Synthetic_CZ", "YOP|RD|AA": "08_Synthetic_CZ", "YPB|MQ|AA": "08_Synthetic_CZ", "YPB|RD|AA": "08_Synthetic_CZ", "YPPSAN|RD|AA": "08_Synthetic_CZ", "YPPSAN(D)|RD|AA": "08_Synthetic_CZ", "YPPSAN(L)|RD|AA": "08_Synthetic_CZ", "YPS#2|CU|AA": "08_Synthetic_CZ", "YPSAN|RD|AA": "08_Synthetic_CZ", "YPSAN(D)|RD|AA": "08_Synthetic_CZ", "YPSAN(L)|RD|AA": "08_Synthetic_CZ", "YRU#8|RD|AA": "08_Synthetic_CZ", "YRUN|RD|AA": "08_Synthetic_CZ", "YRUN(D)|RD|AA": "08_Synthetic_CZ", "YRUN(L)|RD|AA": "08_Synthetic_CZ", "YSAN|RD|AA": "08_Synthetic_CZ", "YSAN(D)|RD|AA": "08_Synthetic_CZ", "YSAN(L)|RD|AA": "08_Synthetic_CZ", "YWCI(1000)|RD|AA": "08_Synthetic_CZ", "YWSA|RD|AA": "08_Synthetic_CZ", "YYESAN|RD|AA": "08_Synthetic_CZ", "YYESAN(D)|RD|AA": "08_Synthetic_CZ", "YYESAN(L)|RD|AA": "08_Synthetic_CZ", "ZBK|OVCN|AA": "08_Synthetic_CZ", "ZC(M)|OV|AA": "08_Synthetic_CZ", "ZGN(M)|MQ|AA": "08_Synthetic_CZ", "ZPD(M)|OV|AA": "08_Synthetic_CZ", "ZPP(M)|MQ|AA": "08_Synthetic_CZ", "ZTZ(M)|RD|AA": "08_Synthetic_CZ", "ZW|OV|AA": "08_Synthetic_CZ", "ZW|RD|AA@(MADS)": "08_Synthetic_CZ", "ZW|RD|AA@(MS)": "08_Synthetic_CZ", "AVYE|HAPPY FACE#2/ENGRAVE|AA": "09_Special_Cut", "BKA|CUCN|AA": "09_Special_Cut", "BKRT|OCSC|AA": "09_Special_Cut", "CN|CU((INFINITY/FIRE)|AA": "09_Special_Cut", "CN|HAPPY FACE#2|AA": "09_Special_Cut", "CN|HAPPY FACE#2/ENGRAVE|AA": "09_Special_Cut", "CN|RE|AA": "09_Special_Cut", "CN|TBCN|AA": "09_Special_Cut", "JD|FANCY|AA": "09_Special_Cut", "JDM|BDT|AA": "09_Special_Cut", "JDM|FANCY|AA": "09_Special_Cut", "JP|OV(INFINITY)|AA": "09_Special_Cut", "JP|OV(WATER)|AA": "09_Special_Cut", "LAVA|BDT|AA": "09_Special_Cut", "LP|CU(W/E)|AA": "09_Special_Cut", "LP|HAPPY FACE#2/ENGRAVE|AA": "09_Special_Cut", "LP|HAPPY FACE#2/STAR|AA": "09_Special_Cut", "LPM|FANCY|AA": "09_Special_Cut", "LRM|PRCB|AA": "09_Special_Cut", "LRM|RD2CB|AA": "09_Special_Cut", "MA|OV|AA": "09_Special_Cut", "MLC|HAPPY FACE#2|AA": "09_Special_Cut", "MOI|KITE|AA": "09_Special_Cut", "OP|RDCB|AA": "09_Special_Cut", "OX|FANCY#CURVE|AA": "09_Special_Cut", "OX|OVCB|AA": "09_Special_Cut", "RBMN|OV|AA": "09_Special_Cut", "ROQ|OC2BR|AA": "09_Special_Cut", "TGE|RDCB|AA": "09_Special_Cut", "TLOP-AU|PRCB|AA": "09_Special_Cut", "TQ|BGCN|AA": "09_Special_Cut", "TQ|FANCY|AA": "09_Special_Cut", "TQ|FANCY(L)|AA": "09_Special_Cut", "TQ|FANCY(R)|AA": "09_Special_Cut", "TQ|HAPPY FACE#2/ENGRAVE|AA": "09_Special_Cut", "TQ-AU|OVCBH|AA": "09_Special_Cut", "TQM|BDT|AA": "09_Special_Cut", "TQM|FANCY|AA": "09_Special_Cut", "TQM|SQCN|AA": "09_Special_Cut", "WMOP|RDCB|AA": "09_Special_Cut", "WMOP|RDCN(ENGRAVE)|AA": "09_Special_Cut", "WOP-AU|RDCB|AA": "09_Special_Cut", "AQ(DYE)|FANCY|B": "09_Special_Cut", "JD|BAT|B": "09_Special_Cut", "JD|FANCY|B": "09_Special_Cut", "JD|FANCY(L)|B": "09_Special_Cut", "JD|FANCY(R)|B": "09_Special_Cut", "JD|HAPPY FACE#2/ENGRAVE|B": "09_Special_Cut", "RDN|HAPPY FACE#1|B": "09_Special_Cut", "RDN|HAPPY FACE#2/ENGRAVE|B": "09_Special_Cut", "RDN|TBCN|B": "09_Special_Cut", "TQ|BAT|B": "09_Special_Cut", "WFP|BAT|B": "09_Special_Cut", "WFP|OVT|A": "09_Special_Cut", "WFP|RDT|A": "09_Special_Cut", "WFP(N)|RD|A": "09_Special_Cut", "YEM|OCRAD|A": "09_Special_Cut", "WFP|BA|AA": "10_Pearl", "WFP|BP|AA": "10_Pearl", "WFP|DR|AA": "10_Pearl", "WFP|RD|AA": "10_Pearl", "WFP|RD|AAA": "10_Pearl", "WFP|RDH|AA": "10_Pearl", "CSA|OV|AA": "11_Ruby_Sapphire", "CSA|RD|AA": "11_Ruby_Sapphire", "GRSA|BG|AA": "11_Ruby_Sapphire", "GRSA|OV|AA": "11_Ruby_Sapphire", "GRSA|PR|AA": "11_Ruby_Sapphire", "GRSA|RD|AA": "11_Ruby_Sapphire", "GRSA-AU|MQ|AA": "11_Ruby_Sapphire", "GRSA-AU|RD|AA": "11_Ruby_Sapphire", "OSA|BG|AA@JA": "11_Ruby_Sapphire", "OSA|BG|AA": "11_Ruby_Sapphire", "OSA|PC|AA": "11_Ruby_Sapphire", "OSA|RD|AA": "11_Ruby_Sapphire", "PSA|BG|AA": "11_Ruby_Sapphire", "PSA|MQ|AA": "11_Ruby_Sapphire", "PSA|PR|AA": "11_Ruby_Sapphire", "PSA|RD|AA": "11_Ruby_Sapphire", "PSA(300)|BG|AA": "11_Ruby_Sapphire", "PSA(300)|RD|AA": "11_Ruby_Sapphire", "PSA(500)|BG|AA": "11_Ruby_Sapphire", "PSA(500)|RD|AA": "11_Ruby_Sapphire", "PSA(D)|BG|AA": "11_Ruby_Sapphire", "PSA(D)|OV|AA": "11_Ruby_Sapphire", "PSA(D)|PC|AA": "11_Ruby_Sapphire", "PSA(L)|BG|AA": "11_Ruby_Sapphire", "PSA(L)|RD|AA": "11_Ruby_Sapphire", "RU|BG|AA": "11_Ruby_Sapphire", "RU|MQ|AA": "11_Ruby_Sapphire", "RU|RD|AA": "11_Ruby_Sapphire", "RU|RD|AAA": "11_Ruby_Sapphire", "RU(100)|BG|AA": "11_Ruby_Sapphire", "RU(100)|RD|AA": "11_Ruby_Sapphire", "RU(D)|BG|AAA": "11_Ruby_Sapphire", "RU(L)|RD|AA": "11_Ruby_Sapphire", "SA|BG|AA": "11_Ruby_Sapphire", "SA|PC|AA": "11_Ruby_Sapphire", "SA|RD|AA": "11_Ruby_Sapphire", "SA|RD|AAA": "11_Ruby_Sapphire", "SA(L)|PR|AA": "11_Ruby_Sapphire", "SA(L)|RD|AA": "11_Ruby_Sapphire", "SA-AU|BG|AA": "11_Ruby_Sapphire", "SA-AU|RD|AA": "11_Ruby_Sapphire", "SAYE|BG|AA@JA": "11_Ruby_Sapphire", "SAYE|RD|AA": "11_Ruby_Sapphire", "SAYE(L)|BG|AA": "11_Ruby_Sapphire", "PSA|RD|A": "11_Ruby_Sapphire", "RU|RD|G": "11_Ruby_Sapphire", "RU|RD|A": "11_Ruby_Sapphire", "RU|RD|B": "11_Ruby_Sapphire", "RU|RD|G1": "11_Ruby_Sapphire", "SA-AU|PR|B": "11_Ruby_Sapphire", "SA-AU|RD|B": "11_Ruby_Sapphire", "SA-AU|TR|B": "11_Ruby_Sapphire"}, "pairs": {"AM|OC": ["01_Colour_Stones"], "AM|PR2BR": ["01_Colour_Stones"], "AM|RD": ["01_Colour_Stones"], "AM|RD2FC": ["01_Colour_Stones"], "AM|SQ": ["01_Colour_Stones"], "AM(D)|RD": ["01_Colour_Stones"], "CT(L)|RD": ["01_Colour_Stones"], "CT(M)|BG": ["01_Colour_Stones"], "CT(M)|MQ": ["01_Colour_Stones"], "CT(M)|OC": ["01_Colour_Stones"], "CT(M)|OV": ["01_Colour_Stones"], "CT(M)|OVBRF": ["01_Colour_Stones"], "CT(M)|PC": ["01_Colour_Stones"], "CT(M)|RD": ["01_Colour_Stones"], "GN|BG": ["01_Colour_Stones"], "GN|OV2BR": ["01_Colour_Stones"], "GN|PR2BR": ["01_Colour_Stones"], "GN|PRBR": ["01_Colour_Stones"], "GN|RD": ["01_Colour_Stones"], "GN|RD2FC": ["01_Colour_Stones"], "GN(P)|SQ": ["01_Colour_Stones"], "GRAM|KT(SPELLBOUND)": ["01_Colour_Stones"], "GRAM|OCH": ["01_Colour_Stones"], "LBT|BG": ["01_Colour_Stones"], "LBT|CUCB": ["01_Colour_Stones"], "LBT|OC": ["01_Colour_Stones"], "LBT|OV(SPECIAL CUT)": ["01_Colour_Stones"], "LBT|OV2BR": ["01_Colour_Stones"], "LBT|PR": ["01_Colour_Stones"], "LBT|RD": ["01_Colour_Stones"], "LBT|RD2BR": ["01_Colour_Stones"], "LSBT|BG": ["01_Colour_Stones"], "LSBT|PR": ["01_Colour_Stones"], "LSBT|RD": ["01_Colour_Stones"], "LSBT|RD2FC": ["01_Colour_Stones"], "LSBT|SQ": ["01_Colour_Stones"], "PAM|OC": ["01_Colour_Stones"], "PAM|PR": ["01_Colour_Stones"], "PAM|RD": ["01_Colour_Stones"], "PD|BG": ["01_Colour_Stones"], "PD|OVBRF": ["01_Colour_Stones"], "PD|PR": ["01_Colour_Stones"], "PD|RD": ["01_Colour_Stones"], "PD|RD2BR": ["01_Colour_Stones"], "PD|RD2FC": ["01_Colour_Stones"], "PD|SQ": ["01_Colour_Stones"], "RHGN|CU": ["01_Colour_Stones"], "RHGN|MQ": ["01_Colour_Stones"], "RHGN|OC": ["01_Colour_Stones"], "RHGN|OV": ["01_Colour_Stones"], "RHGN|OVBRF": ["01_Colour_Stones"], "RHGN|PR": ["01_Colour_Stones"], "RHGN|PRCB": ["01_Colour_Stones"], "RHGN|RD": ["01_Colour_Stones"], "RHGN|RD2FC": ["01_Colour_Stones"], "RHGN|RDCB": ["01_Colour_Stones"], "RHGN|SQ": ["01_Colour_Stones"], "SBT|BG": ["01_Colour_Stones"], "SBT|OV2BR": ["01_Colour_Stones"], "SBT|PR2BR": ["01_Colour_Stones"], "SBT|RD": ["01_Colour_Stones"], "SBT|RD2BR": ["01_Colour_Stones"], "SWBT|CU": ["01_Colour_Stones"], "SWBT|PR": ["01_Colour_Stones"], "SWBT|RD": ["01_Colour_Stones"], "WT|HT": ["01_Colour_Stones"], "WT|RD": ["01_Colour_Stones"], "WZC|RD": ["01_Colour_Stones"], "AM|OCRAD": ["01_Colour_Stones"], "AM|OV(SPECIAL CUT)": ["01_Colour_Stones"], "LSBT|PR2BR": ["01_Colour_Stones"], "AQ|BG": ["02_AQ_GRT_IO_MON_PT_TZ"], "AX|RD": ["02_AQ_GRT_IO_MON_PT_TZ"], "GRT|PR": ["02_AQ_GRT_IO_MON_PT_TZ"], "GRT(L)|RD": ["02_AQ_GRT_IO_MON_PT_TZ"], "IO|TRCB": ["02_AQ_GRT_IO_MON_PT_TZ"], "MON|BG": ["02_AQ_GRT_IO_MON_PT_TZ"], "PMON|PR": ["02_AQ_GRT_IO_MON_PT_TZ"], "PT|BG": ["02_AQ_GRT_IO_MON_PT_TZ"], "PT|OC": ["02_AQ_GRT_IO_MON_PT_TZ"], "PT|PR": ["02_AQ_GRT_IO_MON_PT_TZ"], "PT|RD": ["02_AQ_GRT_IO_MON_PT_TZ"], "PT(L)|RD": ["02_AQ_GRT_IO_MON_PT_TZ"], "TZ|BG": ["02_AQ_GRT_IO_MON_PT_TZ"], "TZ|RD": ["02_AQ_GRT_IO_MON_PT_TZ"], "PTSA-AU|MQ": ["03_PTSA_TBSA_AU"], "PTSA-AU|RD": ["03_PTSA_TBSA_AU"], "TBSA-AU|CU": ["03_PTSA_TBSA_AU"], "BKSA|RD": ["04_BKSA"], "BKSA|TR": ["04_BKSA"], "TS|BG": ["05_TS"], "TS|RD": ["05_TS"], "TS(D)|BG": ["05_TS"], "TS(D)|OC": ["05_TS"], "TS(D)|RD": ["05_TS"], "TS(L)|RD": ["05_TS"], "AM|MQ": ["06_Customer"], "GFP|RD": ["06_Customer"], "PFP|RD": ["06_Customer"], "SA|CU": ["06_Customer"], "SA|OV": ["06_Customer"], "SA|RD": ["06_Customer", "11_Ruby_Sapphire"], "SA|SQ": ["06_Customer"], "WFP|RD": ["06_Customer", "10_Pearl"], "EM|MQ": ["07_EM"], "EM|RD": ["07_EM"], "YAQ#106|MQ": ["08_Synthetic_CZ"], "YBLS#34|PR2BR": ["08_Synthetic_CZ"], "YBLS#34|RD": ["08_Synthetic_CZ"], "YEM|OV": ["08_Synthetic_CZ"], "YEM|OV2BR": ["08_Synthetic_CZ"], "YEM|PR2BR": ["08_Synthetic_CZ"], "YEM|RD": ["08_Synthetic_CZ"], "YEM(1101)|RD": ["08_Synthetic_CZ"], "YEMN|RD": ["08_Synthetic_CZ"], "YEMN(D)|RD": ["08_Synthetic_CZ"], "YEMN(L)|RD": ["08_Synthetic_CZ"], "YOP|CU": ["08_Synthetic_CZ"], "YOP|MQ": ["08_Synthetic_CZ"], "YOP|RD": ["08_Synthetic_CZ"], "YPB|MQ": ["08_Synthetic_CZ"], "YPB|RD": ["08_Synthetic_CZ"], "YPPSAN|RD": ["08_Synthetic_CZ"], "YPPSAN(D)|RD": ["08_Synthetic_CZ"], "YPPSAN(L)|RD": ["08_Synthetic_CZ"], "YPS#2|CU": ["08_Synthetic_CZ"], "YPSAN|RD": ["08_Synthetic_CZ"], "YPSAN(D)|RD": ["08_Synthetic_CZ"], "YPSAN(L)|RD": ["08_Synthetic_CZ"], "YRU#8|RD": ["08_Synthetic_CZ"], "YRUN|RD": ["08_Synthetic_CZ"], "YRUN(D)|RD": ["08_Synthetic_CZ"], "YRUN(L)|RD": ["08_Synthetic_CZ"], "YSAN|RD": ["08_Synthetic_CZ"], "YSAN(D)|RD": ["08_Synthetic_CZ"], "YSAN(L)|RD": ["08_Synthetic_CZ"], "YWCI(1000)|RD": ["08_Synthetic_CZ"], "YWSA|RD": ["08_Synthetic_CZ"], "YYESAN|RD": ["08_Synthetic_CZ"], "YYESAN(D)|RD": ["08_Synthetic_CZ"], "YYESAN(L)|RD": ["08_Synthetic_CZ"], "ZBK|OVCN": ["08_Synthetic_CZ"], "ZC(M)|OV": ["08_Synthetic_CZ"], "ZGN(M)|MQ": ["08_Synthetic_CZ"], "ZPD(M)|OV": ["08_Synthetic_CZ"], "ZPP(M)|MQ": ["08_Synthetic_CZ"], "ZTZ(M)|RD": ["08_Synthetic_CZ"], "ZW|OV": ["08_Synthetic_CZ"], "ZW|RD": ["08_Synthetic_CZ"], "AVYE|HAPPY FACE#2/ENGRAVE": ["09_Special_Cut"], "BKA|CUCN": ["09_Special_Cut"], "BKRT|OCSC": ["09_Special_Cut"], "CN|CU((INFINITY/FIRE)": ["09_Special_Cut"], "CN|HAPPY FACE#2": ["09_Special_Cut"], "CN|HAPPY FACE#2/ENGRAVE": ["09_Special_Cut"], "CN|RE": ["09_Special_Cut"], "CN|TBCN": ["09_Special_Cut"], "JD|FANCY": ["09_Special_Cut"], "JDM|BDT": ["09_Special_Cut"], "JDM|FANCY": ["09_Special_Cut"], "JP|OV(INFINITY)": ["09_Special_Cut"], "JP|OV(WATER)": ["09_Special_Cut"], "LAVA|BDT": ["09_Special_Cut"], "LP|CU(W/E)": ["09_Special_Cut"], "LP|HAPPY FACE#2/ENGRAVE": ["09_Special_Cut"], "LP|HAPPY FACE#2/STAR": ["09_Special_Cut"], "LPM|FANCY": ["09_Special_Cut"], "LRM|PRCB": ["09_Special_Cut"], "LRM|RD2CB": ["09_Special_Cut"], "MA|OV": ["09_Special_Cut"], "MLC|HAPPY FACE#2": ["09_Special_Cut"], "MOI|KITE": ["09_Special_Cut"], "OP|RDCB": ["09_Special_Cut"], "OX|FANCY#CURVE": ["09_Special_Cut"], "OX|OVCB": ["09_Special_Cut"], "RBMN|OV": ["09_Special_Cut"], "ROQ|OC2BR": ["09_Special_Cut"], "TGE|RDCB": ["09_Special_Cut"], "TLOP-AU|PRCB": ["09_Special_Cut"], "TQ|BGCN": ["09_Special_Cut"], "TQ|FANCY": ["09_Special_Cut"], "TQ|FANCY(L)": ["09_Special_Cut"], "TQ|FANCY(R)": ["09_Special_Cut"], "TQ|HAPPY FACE#2/ENGRAVE": ["09_Special_Cut"], "TQ-AU|OVCBH": ["09_Special_Cut"], "TQM|BDT": ["09_Special_Cut"], "TQM|FANCY": ["09_Special_Cut"], "TQM|SQCN": ["09_Special_Cut"], "WMOP|RDCB": ["09_Special_Cut"], "WMOP|RDCN(ENGRAVE)": ["09_Special_Cut"], "WOP-AU|RDCB": ["09_Special_Cut"], "AQ(DYE)|FANCY": ["09_Special_Cut"], "JD|BAT": ["09_Special_Cut"], "JD|FANCY(L)": ["09_Special_Cut"], "JD|FANCY(R)": ["09_Special_Cut"], "JD|HAPPY FACE#2/ENGRAVE": ["09_Special_Cut"], "RDN|HAPPY FACE#1": ["09_Special_Cut"], "RDN|HAPPY FACE#2/ENGRAVE": ["09_Special_Cut"], "RDN|TBCN": ["09_Special_Cut"], "TQ|BAT": ["09_Special_Cut"], "WFP|BAT": ["09_Special_Cut"], "WFP|OVT": ["09_Special_Cut"], "WFP|RDT": ["09_Special_Cut"], "WFP(N)|RD": ["09_Special_Cut"], "YEM|OCRAD": ["09_Special_Cut"], "WFP|BA": ["10_Pearl"], "WFP|BP": ["10_Pearl"], "WFP|DR": ["10_Pearl"], "WFP|RDH": ["10_Pearl"], "CSA|OV": ["11_Ruby_Sapphire"], "CSA|RD": ["11_Ruby_Sapphire"], "GRSA|BG": ["11_Ruby_Sapphire"], "GRSA|OV": ["11_Ruby_Sapphire"], "GRSA|PR": ["11_Ruby_Sapphire"], "GRSA|RD": ["11_Ruby_Sapphire"], "GRSA-AU|MQ": ["11_Ruby_Sapphire"], "GRSA-AU|RD": ["11_Ruby_Sapphire"], "OSA|BG": ["11_Ruby_Sapphire"], "OSA|PC": ["11_Ruby_Sapphire"], "OSA|RD": ["11_Ruby_Sapphire"], "PSA|BG": ["11_Ruby_Sapphire"], "PSA|MQ": ["11_Ruby_Sapphire"], "PSA|PR": ["11_Ruby_Sapphire"], "PSA|RD": ["11_Ruby_Sapphire"], "PSA(300)|BG": ["11_Ruby_Sapphire"], "PSA(300)|RD": ["11_Ruby_Sapphire"], "PSA(500)|BG": ["11_Ruby_Sapphire"], "PSA(500)|RD": ["11_Ruby_Sapphire"], "PSA(D)|BG": ["11_Ruby_Sapphire"], "PSA(D)|OV": ["11_Ruby_Sapphire"], "PSA(D)|PC": ["11_Ruby_Sapphire"], "PSA(L)|BG": ["11_Ruby_Sapphire"], "PSA(L)|RD": ["11_Ruby_Sapphire"], "RU|BG": ["11_Ruby_Sapphire"], "RU|MQ": ["11_Ruby_Sapphire"], "RU|RD": ["11_Ruby_Sapphire"], "RU(100)|BG": ["11_Ruby_Sapphire"], "RU(100)|RD": ["11_Ruby_Sapphire"], "RU(D)|BG": ["11_Ruby_Sapphire"], "RU(L)|RD": ["11_Ruby_Sapphire"], "SA|BG": ["11_Ruby_Sapphire"], "SA|PC": ["11_Ruby_Sapphire"], "SA(L)|PR": ["11_Ruby_Sapphire"], "SA(L)|RD": ["11_Ruby_Sapphire"], "SA-AU|BG": ["11_Ruby_Sapphire"], "SA-AU|RD": ["11_Ruby_Sapphire"], "SAYE|BG": ["11_Ruby_Sapphire"], "SAYE|RD": ["11_Ruby_Sapphire"], "SAYE(L)|BG": ["11_Ruby_Sapphire"], "SA-AU|PR": ["11_Ruby_Sapphire"], "SA-AU|TR": ["11_Ruby_Sapphire"]}, "stones": {"AM": ["01_Colour_Stones"], "AM(D)": ["01_Colour_Stones"], "CT(L)": ["01_Colour_Stones"], "CT(M)": ["01_Colour_Stones"], "GN": ["01_Colour_Stones"], "GN(P)": ["01_Colour_Stones"], "GRAM": ["01_Colour_Stones"], "LBT": ["01_Colour_Stones"], "LSBT": ["01_Colour_Stones"], "PAM": ["01_Colour_Stones"], "PD": ["01_Colour_Stones"], "RHGN": ["01_Colour_Stones"], "SBT": ["01_Colour_Stones"], "SWBT": ["01_Colour_Stones"], "WT": ["01_Colour_Stones"], "WZC": ["01_Colour_Stones"], "AQ": ["02_AQ_GRT_IO_MON_PT_TZ"], "AX": ["02_AQ_GRT_IO_MON_PT_TZ"], "GRT": ["02_AQ_GRT_IO_MON_PT_TZ"], "GRT(L)": ["02_AQ_GRT_IO_MON_PT_TZ"], "IO": ["02_AQ_GRT_IO_MON_PT_TZ"], "MON": ["02_AQ_GRT_IO_MON_PT_TZ"], "PMON": ["02_AQ_GRT_IO_MON_PT_TZ"], "PT": ["02_AQ_GRT_IO_MON_PT_TZ"], "PT(L)": ["02_AQ_GRT_IO_MON_PT_TZ"], "TZ": ["02_AQ_GRT_IO_MON_PT_TZ"], "PTSA-AU": ["03_PTSA_TBSA_AU"], "TBSA-AU": ["03_PTSA_TBSA_AU"], "BKSA": ["04_BKSA"], "TS": ["05_TS"], "TS(D)": ["05_TS"], "TS(L)": ["05_TS"], "EM": ["07_EM"], "YAQ#106": ["08_Synthetic_CZ"], "YBLS#34": ["08_Synthetic_CZ"], "YEM": ["08_Synthetic_CZ"], "YEM(1101)": ["08_Synthetic_CZ"], "YEMN": ["08_Synthetic_CZ"], "YEMN(D)": ["08_Synthetic_CZ"], "YEMN(L)": ["08_Synthetic_CZ"], "YOP": ["08_Synthetic_CZ"], "YPB": ["08_Synthetic_CZ"], "YPPSAN": ["08_Synthetic_CZ"], "YPPSAN(D)": ["08_Synthetic_CZ"], "YPPSAN(L)": ["08_Synthetic_CZ"], "YPS#2": ["08_Synthetic_CZ"], "YPSAN": ["08_Synthetic_CZ"], "YPSAN(D)": ["08_Synthetic_CZ"], "YPSAN(L)": ["08_Synthetic_CZ"], "YRU#8": ["08_Synthetic_CZ"], "YRUN": ["08_Synthetic_CZ"], "YRUN(D)": ["08_Synthetic_CZ"], "YRUN(L)": ["08_Synthetic_CZ"], "YSAN": ["08_Synthetic_CZ"], "YSAN(D)": ["08_Synthetic_CZ"], "YSAN(L)": ["08_Synthetic_CZ"], "YWCI(1000)": ["08_Synthetic_CZ"], "YWSA": ["08_Synthetic_CZ"], "YYESAN": ["08_Synthetic_CZ"], "YYESAN(D)": ["08_Synthetic_CZ"], "YYESAN(L)": ["08_Synthetic_CZ"], "ZBK": ["08_Synthetic_CZ"], "ZC(M)": ["08_Synthetic_CZ"], "ZGN(M)": ["08_Synthetic_CZ"], "ZPD(M)": ["08_Synthetic_CZ"], "ZPP(M)": ["08_Synthetic_CZ"], "ZTZ(M)": ["08_Synthetic_CZ"], "ZW": ["08_Synthetic_CZ"], "WFP": ["10_Pearl"], "CSA": ["11_Ruby_Sapphire"], "GRSA": ["11_Ruby_Sapphire"], "GRSA-AU": ["11_Ruby_Sapphire"], "OSA": ["11_Ruby_Sapphire"], "PSA": ["11_Ruby_Sapphire"], "PSA(300)": ["11_Ruby_Sapphire"], "PSA(500)": ["11_Ruby_Sapphire"], "PSA(D)": ["11_Ruby_Sapphire"], "PSA(L)": ["11_Ruby_Sapphire"], "RU": ["11_Ruby_Sapphire"], "RU(100)": ["11_Ruby_Sapphire"], "RU(D)": ["11_Ruby_Sapphire"], "RU(L)": ["11_Ruby_Sapphire"], "SA": ["11_Ruby_Sapphire"], "SA(L)": ["11_Ruby_Sapphire"], "SA-AU": ["11_Ruby_Sapphire"], "SAYE": ["11_Ruby_Sapphire"], "SAYE(L)": ["11_Ruby_Sapphire"]}, "cuts": ["BA", "BG", "BP", "CU", "CUCB", "DR", "HT", "KT(SPELLBOUND)", "MQ", "OC", "OCH", "OCRAD", "OV", "OV(SPECIAL CUT)", "OV2BR", "OVBRF", "OVCN", "PC", "PR", "PR2BR", "PRBR", "PRCB", "RD", "RD2BR", "RD2FC", "RDCB", "RDH", "SQ", "TR", "TRCB"], "layout": [["01_Colour_Stones"], ["02_AQ_GRT_IO_MON_PT_TZ", "03_PTSA_TBSA_AU", "04_BKSA", "05_TS", "06_Customer", "07_EM", "08_Synthetic_CZ"], ["09_Special_Cut", "10_Pearl"], ["11_Ruby_Sapphire"]]}')

def supplier_sort_key(row):
    # Preserve the full grade, including supplier/customer qualifiers.
    grade = str(row['Grade']).strip()
    prefix = grade.split('@', 1)[0].upper()
    priority = {'AA': 0, 'AAA': 1, 'A': 2, 'B': 3}.get(prefix, 4)
    dimensions = tuple(float(x) for x in re.findall(r'\d+(?:\.\d+)?', str(row['Size'])))
    return priority, grade, str(row['Stone']), str(row['Cut']), dimensions, str(row['Size'])


def supplier_group(row, name_mapping):
    stone = str(row['Stone']).strip().upper()
    # Resolve displayed full names back to source codes only when unambiguous.
    aliases = [str(k).upper() for k, v in name_mapping.items()
               if str(v).strip().upper() == stone]
    if len(aliases) == 1:
        stone = aliases[0]
    elif len(aliases) > 1:
        return '09_Special_Cut'
    cut = str(row['Cut']).strip().upper()
    grade = str(row['Grade']).strip()
    if grade.upper().startswith('CUSTOMER'):
        return '06_Customer'
    exact = SUPPLIER_RULES['exact'].get('|'.join([stone, cut, grade]))
    if exact:
        return exact
    pair = SUPPLIER_RULES['pairs'].get(stone + '|' + cut, [])
    if '09_Special_Cut' in pair:
        return '09_Special_Cut'
    if cut not in SUPPLIER_RULES['cuts']:
        return '09_Special_Cut'
    if len(pair) == 1 and pair[0] != '06_Customer':
        return pair[0]
    candidates = SUPPLIER_RULES['stones'].get(stone, [])
    if not candidates:
        # Allow colour/lot variants of known codes, never guess by substring.
        base = re.split(r'[\(#]', stone, maxsplit=1)[0]
        candidates = sorted(set(group for code, groups in SUPPLIER_RULES['stones'].items()
                                if re.split(r'[\(#]', code, maxsplit=1)[0] == base
                                for group in groups))
    return candidates[0] if len(candidates) == 1 else '09_Special_Cut'


def prepare_supplier_rows(df_aa, df_non_aa, name_mapping):
    rows = []
    for index, record in enumerate(pd.concat([df_aa, df_non_aa], ignore_index=True).to_dict('records'), 1):
        if all(pd.isna(record.get(k)) or str(record.get(k)).strip() == ''
               for k in ['Stone', 'Cut', 'Size', 'PCS', 'Grade']):
            continue
        for key in ['Stone', 'Cut', 'Size', 'Grade']:
            if pd.isna(record.get(key)) or not str(record[key]).strip():
                raise ValueError(f'รายการที่ {index}: กรุณาระบุ {key}')
            record[key] = str(record[key]).strip()
        try:
            pcs = float(record['PCS'])
            if not math.isfinite(pcs) or pcs <= 0 or not pcs.is_integer():
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError(f'รายการที่ {index}: PCS ต้องเป็นจำนวนเต็มมากกว่า 0')
        record['PCS'] = int(pcs)
        record['Group'] = supplier_group(record, name_mapping)
        rows.append(record)
    return pd.DataFrame(rows, columns=['Group', 'Stone', 'Cut', 'Size', 'PCS', 'Grade'])


def supplier_workbook(groups, combined=False):
    """Use the existing app's xlsxwriter dependency; no template file is needed at runtime."""
    import xlsxwriter
    output = io.BytesIO()
    with xlsxwriter.Workbook(output, {'in_memory': True,
                                     'strings_to_formulas': False,
                                     'strings_to_urls': False}) as book:
        sheet = book.add_worksheet('Supplier_Order')
        sheet.hide_gridlines(2)
        title = book.add_format({'bold': True, 'font_size': 11, 'bg_color': '#D9EAF7', 'border': 1})
        head = book.add_format({'bold': True, 'border': 1, 'align': 'center', 'bg_color': '#E7E6E6'})
        text_fmt = book.add_format({'border': 1, 'align': 'center', 'valign': 'vcenter', 'num_format': '@'})
        num_fmt = book.add_format({'border': 1, 'align': 'center', 'num_format': '0'})
        columns = ['Stone', 'Cut', 'Size', 'PCS', 'Grade']
        lanes = SUPPLIER_RULES['layout'] if combined else [list(groups)]
        max_row, max_col = 0, 4
        for lane, names in enumerate(lanes):
            col, row_number = lane * 6, 0
            lane_rows = [r for name in names for r in groups.get(name, [])]
            if not lane_rows:
                continue
            for offset, key in enumerate(columns):
                longest = max([len(str(r[key])) for r in lane_rows] + [len(key)])
                sheet.set_column(col + offset, col + offset, max(9, min(42, longest + 3)))
            sheet.set_column(col + 5, col + 5, 3)
            for name in names:
                records = groups.get(name, [])
                if not records:
                    continue
                sheet.merge_range(row_number, col, row_number, col + 4, name, title)
                row_number += 1
                sheet.write_row(row_number, col, columns, head)
                row_number += 1
                previous_grade = None
                for record in sorted(records, key=supplier_sort_key):
                    if previous_grade is not None and previous_grade != record['Grade']:
                        row_number += 1
                    for offset, key in enumerate(columns):
                        sheet.write(row_number, col + offset, record[key], num_fmt if key == 'PCS' else text_fmt)
                    sheet.set_row(row_number, 21)
                    previous_grade = record['Grade']
                    row_number += 1
                row_number += 2
            max_row, max_col = max(max_row, row_number), max(max_col, col + 4)
        sheet.freeze_panes(2, 0)
        sheet.set_paper(9)
        if combined:
            sheet.set_landscape()
            sheet.set_paper(8)  # A3 for the four-block overview.
        sheet.fit_to_pages(1, 0)
        sheet.set_margins(0.25, 0.25, 0.35, 0.35)
        sheet.print_area(0, 0, max(1, max_row - 1), max_col)
        sheet.set_footer('&CPage &P / &N')
    return output.getvalue()


def supplier_downloads(dataframe):
    groups = {name: dataframe[dataframe['Group'] == name].to_dict('records')
              for name in SUPPLIER_RULES['groups']}
    groups = {name: rows for name, rows in groups.items() if rows}
    if not groups:
        raise ValueError('ไม่มีรายการที่เลือกสำหรับสั่ง Supplier')
    files = {name + '.xlsx': supplier_workbook({name: rows}) for name, rows in groups.items()}
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return supplier_workbook(groups, combined=True), output.getvalue(), files


def show_supplier_order(df_aa, df_non_aa, name_mapping):
    if not st.checkbox('แยกไฟล์สั่ง Supplier ตามแบบ 111.xlsx', value=False,
                       key='supplier_enabled'):
        return
    st.caption('แบ่งกลุ่มตามตัวอย่าง • แยกเกรดเต็ม • เรียงขนาดจากเล็กไปใหญ่ • Cut/รหัสใหม่เข้ากลุ่ม 09_Special_Cut')
    try:
        prepared = prepare_supplier_rows(df_aa, df_non_aa, name_mapping)
    except ValueError as error:
        st.error(str(error))
        return
    if prepared.empty:
        st.info('ยังไม่มีรายการสำหรับสั่ง Supplier')
        return
    # Reset selections only when source/edited rows actually change.
    signature = hashlib.sha256(prepared.to_json().encode()).hexdigest()[:16]
    prepared.insert(0, 'สั่ง', True)
    st.write('**ติ๊กเลือกรายการที่จะสั่ง และแก้กลุ่มได้ก่อนดาวน์โหลด**')
    selected = st.data_editor(
        prepared, key='supplier_selection_' + signature, hide_index=True,
        use_container_width=True, num_rows='fixed',
        disabled=['Stone', 'Cut', 'Size', 'PCS', 'Grade'],
        column_config={
            'สั่ง': st.column_config.CheckboxColumn('สั่ง', default=True),
            'Group': st.column_config.SelectboxColumn('กลุ่ม Supplier',
                       options=list(SUPPLIER_RULES['groups']), required=True),
        },
    )
    selected = selected[selected['สั่ง'].fillna(False)].copy()
    if selected.empty:
        st.info('กรุณาติ๊กอย่างน้อย 1 รายการ')
        return
    if not selected['Group'].isin(SUPPLIER_RULES['groups']).all():
        st.error('กรุณาระบุกลุ่มให้ครบทุกรายการ')
        return
    st.write(f"เลือก {len(selected)} รายการ รวม {int(selected['PCS'].sum()):,} เม็ด")
    special = selected[selected['Group'] == '09_Special_Cut']
    if not special.empty:
        st.info(f'กลุ่ม Cut พิเศษ/รายการใหม่ {len(special)} รายการ — ตรวจสอบหรือแก้กลุ่มในตารางได้')
    combined, archive, files = supplier_downloads(selected)
    st.download_button('ดาวน์โหลดทั้งหมด — แยกไฟล์ตามกลุ่ม (ZIP)', archive,
                       'Supplier_Orders.zip', 'application/zip', use_container_width=True)
    st.download_button('ดาวน์โหลดตารางรวม 4 ชุดคอลัมน์ตามแบบ 111.xlsx', combined,
                       'Supplier_Overview.xlsx',
                       'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                       use_container_width=True)
    with st.expander('ดาวน์โหลดทีละกลุ่ม'):
        for name, content in files.items():
            st.download_button(name, content, name,
                               'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                               key='download_' + name)


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

upload_column, clear_column = st.columns([5, 1])

with upload_column:
    raw_file = st.file_uploader(
        "อัปโหลดไฟล์ดิบ",
        type=["txt", "tsv"],
        key=f"raw_report_{st.session_state.raw_upload_version}",
    )

with clear_column:
    st.write("")
    st.write("")
    st.button(
        "🧹 ล้างค่า",
        on_click=clear_raw_inputs,
        use_container_width=True,
        help="ล้างไฟล์ดิบ ข้อความที่วาง และผลลัพธ์บนหน้าจอ",
    )

pasted_text = st.text_area(
    "หรือวางข้อความดิบตรงนี้",
    height=140,
    placeholder="วางข้อมูล Outstanding Stones due date ตรงนี้…",
    key="pasted_raw_text",
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
        show_supplier_order(edited_df_aa, edited_df_non_aa, mapping_dict)
