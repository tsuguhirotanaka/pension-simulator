"""
年金最適受給戦略シミュレーター
働きながら年金を受け取るシニア層向け 生涯手取り最大化ツール
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from dataclasses import dataclass
import io
import datetime
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

# ─────────────────────────────────────────
# 定数
# ─────────────────────────────────────────
KAKYUU_MONTHLY = 33_125          # 加給年金 月額 (397,500円/年 ÷ 12) 2024年度
ZAIROU_THRESHOLD = 650_000       # 在職老齢年金 支給停止基準額（月額）2026年4月改正
KURIHIKE_RATE = 0.004            # 繰上げ：月0.4%減
KURISAGE_RATE = 0.007            # 繰下げ：月0.7%増

# ─────────────────────────────────────────
# データクラス
# ─────────────────────────────────────────
@dataclass
class PensionInputs:
    current_age: int
    life_expectancy: int
    kiso_monthly: int            # 老齢基礎年金 月額 (65歳基準)
    kosei_monthly: int           # 老齢厚生年金 月額 (65歳基準)
    kosei_20years: bool          # 厚生年金加入20年以上
    salary_before65: int         # 60〜64歳の月給（標準報酬月額）
    bonus_before65: int          # 60〜64歳の年間賞与
    salary_after65: int          # 65歳以降の月給
    bonus_after65: int           # 65歳以降の年間賞与
    work_end_age: int            # 就労終了年齢
    has_spouse: bool
    spouse_age: int              # 配偶者の現在年齢


# ─────────────────────────────────────────
# 手取り計算（簡易実効税率）
# ─────────────────────────────────────────
def net_rate(annual_gross: float) -> float:
    """年間総収入に対する簡易控除率（税金+社会保険料）"""
    if annual_gross <= 0:
        return 0.0
    elif annual_gross <= 1_500_000:
        return 0.12
    elif annual_gross <= 2_000_000:
        return 0.15
    elif annual_gross <= 3_000_000:
        return 0.17
    elif annual_gross <= 4_000_000:
        return 0.20
    elif annual_gross <= 6_000_000:
        return 0.22
    else:
        return 0.25


# ─────────────────────────────────────────
# 在職老齢年金カット計算
# ─────────────────────────────────────────
def calc_zairou_cut(kosei_base_monthly: float, souhoushu_monthly: float) -> float:
    """在職老齢年金による月額カット額を返す（2026年4月改正: 基準65万円）"""
    excess = kosei_base_monthly + souhoushu_monthly - ZAIROU_THRESHOLD
    if excess <= 0:
        return 0.0
    cut = excess / 2.0
    return min(cut, kosei_base_monthly)  # 厚生年金額を超えてカットされない


# ─────────────────────────────────────────
# メインシミュレーション（1受給開始年齢）
# ─────────────────────────────────────────
def simulate(start_age: int, inp: PensionInputs) -> dict:
    """
    start_age (60〜75) を固定し、current_age〜life_expectancy まで年単位でシミュレート。
    複雑なロジック：
    - 繰上げ/繰下げ増減率
    - 在職老齢年金（受給中 & 繰下げ待機中）
    - 加給年金（繰下げ待機中・全額停止中は不支給）
    """

    # ── 増減率の計算 ──
    months_diff = (start_age - 65) * 12  # 65歳基準、負=繰上げ、正=繰下げ
    if months_diff < 0:
        kiso_rate = months_diff * KURIHIKE_RATE          # 繰上げ（負の値）
        kosei_adj_rate_simple = months_diff * KURIHIKE_RATE
    else:
        kiso_rate = months_diff * KURISAGE_RATE           # 繰下げ（正の値）
        kosei_adj_rate_simple = months_diff * KURISAGE_RATE

    kiso_adjusted = inp.kiso_monthly * (1 + kiso_rate)

    # ── 厚生年金の繰下げ + 在職老齢年金相互作用（65歳〜繰下げ待機期間） ──
    # 「65歳から受給していたとしたら在職老齢年金でカットされる額」を先に算出し、
    # カット後の金額に対してのみ繰下げ増額を適用する
    if start_age > 65:
        souhoushu_65 = inp.salary_after65 + inp.bonus_after65 / 12
        hypothetical_cut = calc_zairou_cut(inp.kosei_monthly, souhoushu_65)
        effective_kosei_base = max(0.0, inp.kosei_monthly - hypothetical_cut)
        kosei_adjusted = effective_kosei_base * (1 + kosei_adj_rate_simple)
        # 全額停止されていた場合の待機期間のカット情報
        waiting_cut_per_month = hypothetical_cut
    else:
        kosei_adjusted = inp.kosei_monthly * (1 + kosei_adj_rate_simple)
        waiting_cut_per_month = 0.0

    records = []
    for age in range(inp.current_age, inp.life_expectancy + 1):
        year_offset = age - inp.current_age

        # ── 就労状況 ──
        is_working = age < inp.work_end_age
        if age < 65:
            salary = inp.salary_before65 if is_working else 0
            bonus = inp.bonus_before65 if is_working else 0
        else:
            salary = inp.salary_after65 if is_working else 0
            bonus = inp.bonus_after65 if is_working else 0
        souhoushu = salary + bonus / 12  # 総報酬月額相当額

        receiving = age >= start_age

        # ── 各月額計算 ──
        monthly_kiso = 0.0
        monthly_kosei = 0.0
        monthly_kakyuu = 0.0
        zairou_cut_month = 0.0

        if receiving:
            monthly_kiso = kiso_adjusted

            # 厚生年金（受給中の在職老齢年金）
            if is_working:
                cut = calc_zairou_cut(kosei_adjusted, souhoushu)
                zairou_cut_month = cut
                monthly_kosei = kosei_adjusted - cut
            else:
                monthly_kosei = kosei_adjusted

            # ── 加給年金の判定 ──
            # 条件: 厚生年金加入20年以上 & 配偶者あり & 本人65歳以上 & 配偶者65歳未満
            # 繰下げ待機中は不支給 → receiving=True かつ age>=65 の場合のみ
            # 在職老齢年金で厚生年金が全額停止の場合も不支給
            if inp.has_spouse and inp.kosei_20years and age >= 65:
                spouse_age_now = inp.spouse_age + year_offset
                if spouse_age_now < 65 and monthly_kosei > 0:
                    monthly_kakyuu = KAKYUU_MONTHLY

        # ── 繰上げ中（age<65）は加給年金なし、65歳以降は上記で処理済み ──

        # ── 年収換算 ──
        annual_pension = (monthly_kiso + monthly_kosei + monthly_kakyuu) * 12
        annual_salary = salary * 12 + bonus
        annual_gross = annual_pension + annual_salary
        annual_net = annual_gross * (1 - net_rate(annual_gross))

        records.append({
            "age": age,
            "receiving": receiving,
            "monthly_kiso": monthly_kiso,
            "monthly_kosei": monthly_kosei,
            "zairou_cut": zairou_cut_month,
            "monthly_kakyuu": monthly_kakyuu,
            "monthly_pension_gross": monthly_kiso + monthly_kosei + monthly_kakyuu,
            "monthly_salary": salary,
            "annual_gross": annual_gross,
            "annual_net": annual_net,
        })

    df = pd.DataFrame(records)
    lifetime_net = df["annual_net"].sum()
    lifetime_pension_gross = df["annual_pension_gross"].sum() if "annual_pension_gross" in df else (
        df["monthly_pension_gross"] * 12).sum()
    lifetime_kakyuu = (df["monthly_kakyuu"] * 12).sum()
    total_zairou_cut = (df["zairou_cut"] * 12).sum()

    return {
        "start_age": start_age,
        "adjustment_rate": kiso_rate,  # 同率
        "kiso_adjusted": kiso_adjusted,
        "kosei_adjusted": kosei_adjusted,
        "waiting_cut_per_month": waiting_cut_per_month,
        "lifetime_net": lifetime_net,
        "lifetime_pension_gross": lifetime_pension_gross,
        "lifetime_kakyuu": lifetime_kakyuu,
        "total_zairou_cut": total_zairou_cut,
        "df": df,
    }


# ─────────────────────────────────────────
# 推奨コメント生成
# ─────────────────────────────────────────
def generate_recommendation(results: list[dict], inp: PensionInputs) -> tuple[int, float, float, str]:
    best = max(results, key=lambda x: x["lifetime_net"])
    best_age = best["start_age"]
    best_net = best["lifetime_net"]

    r65 = next((r for r in results if r["start_age"] == 65), results[0])
    diff_vs_65 = best_net - r65["lifetime_net"]

    lines = []

    if best_age < 65:
        # ── 繰上げが最適 ──
        pct = abs(best["adjustment_rate"]) * 100
        lines.append(
            f"**{best_age}歳からの繰上げ受給**が最も有利です（減額率 −{pct:.1f}%）。"
        )
        if r65["total_zairou_cut"] > 0:
            cut_m = r65["total_zairou_cut"] / max(1, inp.life_expectancy - 65)
            lines.append(
                f"65歳以降に受け取ると、在職老齢年金により生涯で計**約{r65['total_zairou_cut']/10000:.0f}万円**がカットされます"
                f"（平均年{cut_m/10000:.0f}万円）。"
                f"それならば{best_age}歳から働きながら少額でも早めに受け取る方が、"
                f"想定寿命{inp.life_expectancy}歳までの累計では有利になります。"
            )
        else:
            lines.append(
                f"想定寿命{inp.life_expectancy}歳を踏まえると、早期に受給を開始した方が"
                "生涯の累計受取額で上回ります。"
            )
        lines.append(
            "⚠️ **注意**：繰上げ受給は一度選択すると生涯減額が固定されます。"
            "また、事後重症による障害年金の請求ができなくなる点にご留意ください。"
        )

    elif best_age == 65:
        # ── 65歳標準受給が最適 ──
        lines.append("**65歳からの標準受給**が最も有利です。")
        if r65["total_zairou_cut"] < 100_000:
            lines.append("在職老齢年金による大きなカットも発生せず、増減なしで安定的に受給できます。")
        else:
            lines.append(
                "繰下げによる増額よりも、65歳から確実に受け取る方が"
                f"想定寿命{inp.life_expectancy}歳までの総額で優位です。"
            )

    else:
        # ── 繰下げが最適 ──
        pct = best["adjustment_rate"] * 100
        lines.append(
            f"**{best_age}歳からの繰下げ受給**が最も有利です（増額率 +{pct:.1f}%）。"
        )
        if r65["total_zairou_cut"] > 0:
            lines.append(
                f"65歳〜{best_age - 1}歳の期間は在職老齢年金により厚生年金が一部カットされるため、"
                "どうせ減額されるなら繰下げ待機して増額率を積み上げる方が効率的です。"
                "なお、繰下げ待機中に在職老齢年金でカットされていた分は増額の対象外となる"
                "計算ロジックを反映済みです。"
            )
        if inp.life_expectancy >= 82:
            lines.append(
                f"想定寿命{inp.life_expectancy}歳と長めのため、"
                "繰下げ増額の恩恵を長期間享受でき、損益分岐点を超えます。"
            )
        # 加給年金の警告
        if inp.has_spouse and inp.kosei_20years:
            spouse_age_at_65 = inp.spouse_age + (65 - inp.current_age)
            if spouse_age_at_65 < 65:
                loss_years = min(best_age - 65, 65 - spouse_age_at_65)
                if loss_years > 0:
                    lines.append(
                        f"⚠️ 繰下げ待機中は**加給年金（年約40万円）が支給停止**になります。"
                        f"{best_age}歳まで待つことで約{loss_years}年×40万円＝"
                        f"**約{loss_years * 40}万円**の加給年金を受け取れない可能性があります。"
                        "この損失も加味した上で、それでも繰下げが有利という結果です。"
                    )

    if diff_vs_65 != 0:
        sign = "+" if diff_vs_65 > 0 else ""
        lines.append(
            f"\n📊 65歳受給との差：**{sign}{diff_vs_65/10000:.0f}万円**（生涯手取りベース）"
        )

    return best_age, best_net, diff_vs_65, "\n\n".join(lines)


# ─────────────────────────────────────────
# PDF生成
# ─────────────────────────────────────────
def _setup_jp_font():
    """利用可能な日本語フォントを探して設定する"""
    jp_candidates = ["Noto Sans CJK JP", "Noto Sans JP", "IPAexGothic", "IPAPGothic",
                     "Hiragino Sans", "Hiragino Kaku Gothic Pro", "Yu Gothic", "Meiryo"]
    available = {f.name for f in fm.fontManager.ttflist}
    for font in jp_candidates:
        if font in available:
            plt.rcParams["font.family"] = font
            return
    # 見つからない場合はNoto Sans CJKをダウンロードして登録
    try:
        import urllib.request, os, tempfile
        url = "https://github.com/googlefonts/noto-cjk/raw/main/Sans/OTF/Japanese/NotoSansCJKjp-Regular.otf"
        font_path = os.path.join(tempfile.gettempdir(), "NotoSansCJKjp.otf")
        if not os.path.exists(font_path):
            urllib.request.urlretrieve(url, font_path)
        fm.fontManager.addfont(font_path)
        prop = fm.FontProperties(fname=font_path)
        plt.rcParams["font.family"] = prop.get_name()
    except Exception:
        plt.rcParams["font.family"] = "DejaVu Sans"  # フォールバック

def _make_bar_chart_image(all_results: list, best_age: int) -> io.BytesIO:
    """棒グラフをPNG画像としてBytesIOで返す"""
    _setup_jp_font()
    ages = [r["start_age"] for r in all_results]
    nets = [r["lifetime_net"] / 10000 for r in all_results]
    bar_colors = ["#ffe066" if a == best_age else "#6fb3f5" if a == 65 else "#adb5bd" for a in ages]

    fig, ax = plt.subplots(figsize=(10, 4))
    bars = ax.bar([f"{a}歳" for a in ages], nets, color=bar_colors, edgecolor="white", linewidth=0.5)

    for bar, age in zip(bars, ages):
        if age == best_age:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 5,
                    "最適", ha="center", va="bottom", fontsize=9, fontweight="bold", color="#1a3a5c")
        elif age == 65:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 5,
                    "基準", ha="center", va="bottom", fontsize=8, color="#0d6efd")

    ax.set_ylabel("生涯総手取り額（万円）")
    ax.set_title(f"受給開始年齢別 生涯総手取り額（最適={best_age}歳・基準=65歳）")
    ax.set_ylim(min(nets) * 0.95, max(nets) * 1.10)
    ax.yaxis.grid(True, alpha=0.3)
    ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)
    plt.xticks(rotation=45, fontsize=8)
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


def generate_pdf(
    inp: PensionInputs,
    all_results: list,
    best_age: int,
    best_net: float,
    diff_vs_65: float,
    reason_text: str,
) -> bytes:
    """PDFレポートを生成してbytesで返す"""
    # 日本語フォント登録
    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
    FONT = "HeiseiKakuGo-W5"

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm,
        topMargin=18*mm, bottomMargin=18*mm,
    )

    # よく使う色を変数で定義（colors.xxx は環境依存があるため HexColor に統一）
    C_BLACK  = HexColor("#000000")
    C_WHITE  = HexColor("#ffffff")
    C_GREY   = HexColor("#6c757d")
    C_NAVY   = HexColor("#1a3a5c")
    C_BLUE   = HexColor("#0d6efd")
    C_HEAD   = HexColor("#1a3a5c")
    C_LINE   = HexColor("#dee2e6")
    C_ROW0   = HexColor("#ffffff")
    C_ROW1   = HexColor("#f0f4ff")
    C_BEST   = HexColor("#fff3b0")
    C_65     = HexColor("#dbeafe")

    # スタイル定義
    def style(name, size=10, color=None, align="LEFT", leading=None):
        return ParagraphStyle(
            name,
            fontName=FONT,
            fontSize=size,
            textColor=color or C_BLACK,
            alignment={"LEFT": 0, "CENTER": 1, "RIGHT": 2}[align],
            leading=leading or size * 1.5,
            spaceAfter=2,
        )

    s_title  = style("title",  18, color=C_NAVY,  align="CENTER")
    s_sub    = style("sub",     9, color=C_GREY,  align="CENTER")
    s_h1     = style("h1",     13, color=C_NAVY)
    s_body   = style("body",    9)
    s_best   = style("best",   14, color=C_NAVY,  align="CENTER")
    s_small  = style("small",   8, color=C_GREY)

    def hr():
        return Table([[""]], colWidths=[170*mm],
                     style=TableStyle([("LINEBELOW", (0,0), (-1,-1), 0.5, C_LINE)]))

    story = []

    # ── ヘッダー ──
    story.append(Paragraph("年金最適受給戦略レポート", s_title))
    story.append(Paragraph(
        f"生成日：{datetime.date.today().strftime('%Y年%m月%d日')}　／　"
        f"想定寿命：{inp.life_expectancy}歳　／　2026年4月改正対応版", s_sub))
    story.append(Spacer(1, 4*mm))
    story.append(hr())
    story.append(Spacer(1, 3*mm))

    # ── 入力情報 ──
    story.append(Paragraph("■ 入力情報", s_h1))
    story.append(Spacer(1, 2*mm))
    info_data = [
        ["項目", "値", "項目", "値"],
        ["現在の年齢", f"{inp.current_age}歳", "引退予定年齢", f"{inp.work_end_age}歳"],
        ["想定寿命", f"{inp.life_expectancy}歳", "厚生年金加入20年以上", "はい" if inp.kosei_20years else "いいえ"],
        ["老齢基礎年金（月額）", f"{inp.kiso_monthly:,}円", "老齢厚生年金（月額）", f"{inp.kosei_monthly:,}円"],
        ["月給（65歳前）", f"{inp.salary_before65:,}円", "月給（65歳以降）", f"{inp.salary_after65:,}円"],
        ["年間賞与（65歳前）", f"{inp.bonus_before65:,}円", "年間賞与（65歳以降）", f"{inp.bonus_after65:,}円"],
        ["配偶者", "あり" if inp.has_spouse else "なし",
         "配偶者の年齢", f"{inp.spouse_age}歳" if inp.has_spouse else "−"],
    ]
    t = Table(info_data, colWidths=[42*mm, 40*mm, 42*mm, 40*mm])
    info_style = [
        ("FONTNAME",     (0,0), (-1,-1), FONT),
        ("FONTSIZE",     (0,0), (-1,-1), 8.5),
        ("BACKGROUND",   (0,0), (-1,0),  C_HEAD),
        ("TEXTCOLOR",    (0,0), (-1,0),  C_WHITE),
        ("GRID",         (0,0), (-1,-1), 0.3, C_LINE),
        ("VALIGN",       (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",   (0,0), (-1,-1), 3),
        ("BOTTOMPADDING",(0,0), (-1,-1), 3),
    ]
    for i in range(1, len(info_data)):
        info_style.append(("BACKGROUND", (0,i), (-1,i), C_ROW0 if i % 2 == 1 else C_ROW1))
    t.setStyle(TableStyle(info_style))
    story.append(t)
    story.append(Spacer(1, 5*mm))

    # ── 最適受給戦略 ──
    story.append(hr())
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph("■ 最適受給戦略の提案", s_h1))
    story.append(Spacer(1, 2*mm))

    adj = next(r["adjustment_rate"] for r in all_results if r["start_age"] == best_age)
    adj_str = f"+{adj*100:.1f}%" if adj >= 0 else f"{adj*100:.1f}%"
    sign = "+" if diff_vs_65 > 0 else ""

    best_data = [
        [Paragraph("最適受給開始年齢", s_body),
         Paragraph(f"{best_age}歳", s_best),
         Paragraph("年金増減率", s_body),
         Paragraph(adj_str, s_best)],
        [Paragraph("生涯総手取り（推計）", s_body),
         Paragraph(f"約{best_net/10000:.0f}万円", s_best),
         Paragraph("65歳受給との差", s_body),
         Paragraph(f"{sign}{diff_vs_65/10000:.0f}万円", s_best)],
    ]
    bt = Table(best_data, colWidths=[42*mm, 43*mm, 42*mm, 37*mm])
    bt.setStyle(TableStyle([
        ("FONTNAME",     (0,0), (-1,-1), FONT),
        ("BACKGROUND",   (0,0), (-1,-1), HexColor("#eef4ff")),
        ("BOX",          (0,0), (-1,-1), 1.5, C_BLUE),
        ("INNERGRID",    (0,0), (-1,-1), 0.3, HexColor("#c5d8f5")),
        ("VALIGN",       (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",   (0,0), (-1,-1), 5),
        ("BOTTOMPADDING",(0,0), (-1,-1), 5),
    ]))
    story.append(bt)
    story.append(Spacer(1, 3*mm))

    # 推奨理由（マークダウン記号を除去）
    clean_reason = reason_text.replace("**", "").replace("⚠️", "※").replace("📊", "").replace("\n\n", "\n")
    for line in clean_reason.split("\n"):
        if line.strip():
            story.append(Paragraph(line.strip(), s_body))
    story.append(Spacer(1, 5*mm))

    # ── 棒グラフ ──
    story.append(hr())
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph("■ 受給開始年齢別 生涯総手取り額", s_h1))
    story.append(Spacer(1, 2*mm))
    chart_buf = _make_bar_chart_image(all_results, best_age)
    story.append(RLImage(chart_buf, width=165*mm, height=66*mm))
    story.append(Spacer(1, 5*mm))

    # ── 詳細比較テーブル ──
    story.append(hr())
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph("■ 全受給パターン詳細比較", s_h1))
    story.append(Spacer(1, 2*mm))

    r65_net = next(r["lifetime_net"] for r in all_results if r["start_age"] == 65)
    tbl_header = ["受給開始", "増減率", "月額年金(平均)", "在職カット累計", "加給年金累計", "生涯総手取り", "65歳比"]
    tbl_data = [tbl_header]
    for r in all_results:
        sa = r["start_age"]
        recv_df = r["df"][r["df"]["receiving"]]
        avg_m = recv_df["monthly_pension_gross"].mean() if len(recv_df) > 0 else 0
        adj2 = r["adjustment_rate"] * 100
        adj2_str = f"+{adj2:.1f}%" if adj2 > 0 else (f"{adj2:.1f}%" if adj2 < 0 else "±0%")
        diff2 = r["lifetime_net"] - r65_net
        diff2_str = f"+{diff2/10000:.0f}万" if diff2 > 0 else f"{diff2/10000:.0f}万"
        mark = "★最適" if sa == best_age else ("基準" if sa == 65 else "")
        tbl_data.append([
            f"{sa}歳 {mark}",
            adj2_str,
            f"{avg_m:,.0f}円",
            f"{r['total_zairou_cut']/10000:.0f}万円" if r["total_zairou_cut"] > 0 else "−",
            f"{r['lifetime_kakyuu']/10000:.0f}万円" if r["lifetime_kakyuu"] > 0 else "−",
            f"{r['lifetime_net']/10000:.0f}万円",
            diff2_str,
        ])

    col_w = [24*mm, 18*mm, 26*mm, 24*mm, 22*mm, 26*mm, 18*mm]
    dt = Table(tbl_data, colWidths=col_w, repeatRows=1)
    dt_style = [
        ("FONTNAME",     (0,0), (-1,-1), FONT),
        ("FONTSIZE",     (0,0), (-1,-1), 7.5),
        ("BACKGROUND",   (0,0), (-1,0),  C_HEAD),
        ("TEXTCOLOR",    (0,0), (-1,0),  C_WHITE),
        ("GRID",         (0,0), (-1,-1), 0.3, C_LINE),
        ("VALIGN",       (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",   (0,0), (-1,-1), 3),
        ("BOTTOMPADDING",(0,0), (-1,-1), 3),
        ("ALIGN",        (1,1), (-1,-1), "RIGHT"),
    ]
    for i, r in enumerate(all_results, start=1):
        if r["start_age"] == best_age:
            dt_style.append(("BACKGROUND", (0,i), (-1,i), C_BEST))
        elif r["start_age"] == 65:
            dt_style.append(("BACKGROUND", (0,i), (-1,i), C_65))
        else:
            dt_style.append(("BACKGROUND", (0,i), (-1,i), C_ROW0 if i % 2 == 1 else C_ROW1))
    dt.setStyle(TableStyle(dt_style))
    story.append(dt)
    story.append(Spacer(1, 5*mm))

    # ── フッター ──
    story.append(hr())
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        "※ 本レポートは概算値です。実際の年金額・税額は個人の状況により異なります。"
        "正確な試算は日本年金機構またはお近くの社会保険労務士にご相談ください。"
        "　在職老齢年金基準：2026年4月改正後（月65万円）　加給年金：2024年度額（年397,500円）", s_small))

    doc.build(story)
    return buf.getvalue()


# ─────────────────────────────────────────
# ページ設定・スタイル
# ─────────────────────────────────────────
st.set_page_config(
    page_title="年金最適受給戦略シミュレーター",
    page_icon="💴",
    layout="wide",
)

st.markdown("""
<style>
.best-box {
    background: linear-gradient(135deg, #1a3a5c 0%, #0d6efd 100%);
    color: white;
    border-radius: 16px;
    padding: 28px 32px;
    margin-bottom: 24px;
    box-shadow: 0 4px 20px rgba(13,110,253,0.3);
}
.best-box h2 { color: #ffe066; margin: 0 0 8px 0; font-size: 1.6rem; }
.best-box .age-badge {
    display: inline-block;
    background: #ffe066;
    color: #1a3a5c;
    font-size: 2.4rem;
    font-weight: 900;
    border-radius: 50px;
    padding: 4px 28px;
    margin: 8px 0 16px 0;
}
.metric-card {
    background: #f8f9fa;
    border-left: 4px solid #0d6efd;
    border-radius: 8px;
    padding: 14px 18px;
    margin: 6px 0;
}
.warn-box {
    background: #fff3cd;
    border-left: 4px solid #ffc107;
    border-radius: 8px;
    padding: 12px 16px;
    margin: 10px 0;
    font-size: 0.9rem;
}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────
# メインエリア
# ─────────────────────────────────────────
st.title("💴 年金最適受給戦略シミュレーター")
st.caption("在職老齢年金・繰上げ・繰下げ・加給年金をすべて考慮した生涯手取り最大化ツール（2026年4月改正対応）")

# ─────────────────────────────────────────
# 入力フォーム（メイン画面）
# ─────────────────────────────────────────
with st.expander("📝 情報を入力する", expanded=not st.session_state.get("simulated")):
    st.markdown("#### 👤 基本情報")
    col1, col2, col3 = st.columns(3)
    with col1:
        current_age = st.number_input(
            "現在の年齢",
            min_value=50, max_value=74, value=60, step=1,
            help="現在の年齢を入力してください。"
        )
    with col2:
        life_expectancy = st.number_input(
            "想定寿命",
            min_value=65, max_value=100, value=85, step=1,
            help="何歳まで生きると想定するか。平均寿命：男性81歳、女性87歳。長生きリスクを考慮して多めに設定するのが一般的です。"
        )
    with col3:
        work_end_age = st.number_input(
            "引退予定年齢",
            min_value=int(current_age), max_value=80, value=70, step=1,
            help="この年齢から給与収入がゼロになると仮定します。"
        )

    st.divider()
    st.markdown("#### 💴 年金情報（65歳時点）")
    st.caption("ねんきん定期便またはねんきんネットで確認できます。")
    col1, col2 = st.columns(2)
    with col1:
        kiso_monthly = st.number_input(
            "老齢基礎年金 月額（円）",
            min_value=0, max_value=70_000, value=65_000, step=1_000,
            format="%d",
            help="65歳から受け取れる国民年金（老齢基礎年金）の月額。満額は約68,000円（2024年度）。"
        )
    with col2:
        kosei_monthly = st.number_input(
            "老齢厚生年金 月額（円）",
            min_value=0, max_value=400_000, value=120_000, step=5_000,
            format="%d",
            help="65歳から受け取れる厚生年金（老齢厚生年金）の月額。在職老齢年金の対象となります。"
        )
    kosei_20years = st.checkbox(
        "厚生年金加入期間が20年以上",
        value=True,
        help="加給年金（家族手当）の受給条件です。20年未満の場合は加給年金は支給されません。"
    )

    st.divider()
    st.markdown("#### 💼 就労状況（給与）")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**60〜64歳**")
        salary_before65 = st.number_input(
            "月給（円）",
            min_value=0, max_value=1_500_000, value=300_000, step=10_000,
            format="%d", key="sal_b65",
            help="在職老齢年金の計算に使われます。標準報酬月額は通勤手当等を含む月給ベースです。"
        )
        bonus_before65 = st.number_input(
            "年間賞与（円）",
            min_value=0, max_value=10_000_000, value=600_000, step=100_000,
            format="%d", key="bon_b65",
            help="年間の賞与合計額。月割り（÷12）して総報酬月額相当額に加算されます。"
        )
    with col2:
        st.markdown("**65歳以降**")
        salary_after65 = st.number_input(
            "月給（円）",
            min_value=0, max_value=1_500_000, value=250_000, step=10_000,
            format="%d", key="sal_a65",
            help="65歳以降の月給。在職老齢年金（基準：65万円）の計算に使います。"
        )
        bonus_after65 = st.number_input(
            "年間賞与（円）",
            min_value=0, max_value=10_000_000, value=500_000, step=100_000,
            format="%d", key="bon_a65",
        )

    st.divider()
    st.markdown("#### 👫 配偶者情報")
    has_spouse = st.checkbox(
        "配偶者がいる（生計維持）",
        value=True,
        help="加給年金（年約40万円）の受給可否に影響します。"
    )
    if has_spouse:
        spouse_age = st.number_input(
            "配偶者の現在年齢",
            min_value=20, max_value=90, value=57, step=1,
            help="配偶者が65歳になるまで加給年金が支給されます（条件を満たす場合）。"
        )
    else:
        spouse_age = 0

    st.divider()
    run_btn = st.button("🔍 シミュレーション実行", type="primary", use_container_width=True)
    if run_btn:
        st.session_state["simulated"] = True

if not st.session_state.get("simulated"):

    # 用語説明
    with st.expander("📖 主な用語の説明"):
        st.markdown("""
| 用語 | 説明 |
|------|------|
| **繰上げ受給** | 60〜64歳から年金を前倒しで受け取ること。1ヶ月ごとに **−0.4%** 減額（永久）。最大 **−24%** |
| **繰下げ受給** | 66〜75歳に年金を遅らせて受け取ること。1ヶ月ごとに **+0.7%** 増額（永久）。最大 **+84%** |
| **在職老齢年金** | 働きながら年金を受け取る場合、給与と年金の合計が **月65万円（2026年4月改正）** を超えると超過額の半分が厚生年金からカットされる制度 |
| **加給年金** | 厚生年金加入20年以上で、65歳未満の配偶者を扶養している場合に受け取れる家族手当（年約40万円） |
| **振替加算** | 配偶者が65歳になると加給年金の代わりに配偶者の基礎年金に上乗せされる金額 |
| **総報酬月額相当額** | 月給（標準報酬月額）＋ 賞与÷12 の合計額。在職老齢年金の計算に使用 |
        """)
    st.stop()


# ── 入力オブジェクト生成 ──
inp = PensionInputs(
    current_age=int(current_age),
    life_expectancy=int(life_expectancy),
    kiso_monthly=int(kiso_monthly),
    kosei_monthly=int(kosei_monthly),
    kosei_20years=kosei_20years,
    salary_before65=int(salary_before65),
    bonus_before65=int(bonus_before65),
    salary_after65=int(salary_after65),
    bonus_after65=int(bonus_after65),
    work_end_age=int(work_end_age),
    has_spouse=has_spouse,
    spouse_age=int(spouse_age) if has_spouse else 0,
)

# ── 全パターン計算 ──
start_ages = list(range(60, 76))  # 60〜75歳
with st.spinner("計算中..."):
    all_results = [simulate(sa, inp) for sa in start_ages]

# ── 推奨生成 ──
best_age, best_net, diff_vs_65, reason_text = generate_recommendation(all_results, inp)

# 最適の月々年金手取り額を算出（受給開始後の最初の年の月額）
def get_monthly_pension(result):
    recv = result["df"][result["df"]["receiving"]]
    if len(recv) == 0:
        return 0
    row = recv.iloc[0]
    monthly_gross = row["monthly_pension_gross"]
    annual_gross = row["annual_gross"]
    return monthly_gross * (1 - net_rate(annual_gross))

best_monthly = get_monthly_pension(next(r for r in all_results if r["start_age"] == best_age))
monthly_65   = get_monthly_pension(next(r for r in all_results if r["start_age"] == 65))
monthly_diff = best_monthly - monthly_65


# ═══════════════════════════════════════
# [1] 最適受給戦略の提案
# ═══════════════════════════════════════
st.markdown("## 💡 最適受給戦略の提案")

adj_rate = next(r["adjustment_rate"] for r in all_results if r["start_age"] == best_age)
adj_str = f"+{adj_rate*100:.1f}%" if adj_rate >= 0 else f"{adj_rate*100:.1f}%"
diff_sign = "+" if monthly_diff >= 0 else ""

st.markdown(f"""
<div class="best-box">
  <h2>🏆 最もお得な受給開始年齢</h2>
  <div class="age-badge">{best_age}歳</div>
  <div style="font-size:1.1rem; line-height:2.2;">
    月々の年金手取り：<strong style="font-size:1.5rem;">約{best_monthly/10000:.1f}万円</strong>／月
    　<span style="font-size:0.95rem; opacity:0.85;">（65歳比：{diff_sign}{monthly_diff/10000:.1f}万円／月）</span>
  </div>
  <div style="font-size:0.95rem; line-height:1.8; opacity:0.9;">
    年金増減率：<strong>{adj_str}</strong>　／
    65歳より受け取り総額：<strong>{'+' if diff_vs_65>=0 else ''}{diff_vs_65/10000:.0f}万円</strong>多い
  </div>
</div>
""", unsafe_allow_html=True)

st.markdown(reason_text)

# 在職老齢年金の注意表示
souhoushu_after65 = salary_after65 + bonus_after65 / 12
monthly_total_after65 = kosei_monthly + souhoushu_after65
if monthly_total_after65 > ZAIROU_THRESHOLD:
    excess = monthly_total_after65 - ZAIROU_THRESHOLD
    cut_est = min(excess / 2, kosei_monthly)
    st.markdown(f"""
<div class="warn-box">
⚠️ <strong>在職老齢年金のカット試算（65歳・改正後基準）</strong><br>
老齢厚生年金月額（{kosei_monthly:,}円）＋ 総報酬月額相当額（{souhoushu_after65:,.0f}円）
= <strong>{monthly_total_after65:,.0f}円</strong> ＞ 基準額65万円<br>
→ 月額 <strong>約{cut_est:,.0f}円</strong> がカットされます（年間約{cut_est*12/10000:.0f}万円）
</div>
""", unsafe_allow_html=True)

st.divider()


# ═══════════════════════════════════════
# [2] 月々の年金手取り比較グラフ
# ═══════════════════════════════════════
st.markdown("## 📊 受給開始年齢別 月々の年金手取り額")

ages_plot = [r["start_age"] for r in all_results]
monthly_plot = [get_monthly_pension(r) / 10000 for r in all_results]  # 万円/月

bar_colors = []
for r in all_results:
    if r["start_age"] == best_age:
        bar_colors.append("#ffe066")
    elif r["start_age"] == 65:
        bar_colors.append("#6fb3f5")
    else:
        bar_colors.append("#adb5bd")

y_max = max(monthly_plot)
y_min = min(monthly_plot)
y_range_top = y_max + (y_max - y_min) * 0.25

fig_bar = go.Figure(go.Bar(
    x=[f"{a}歳" for a in ages_plot],
    y=monthly_plot,
    marker_color=bar_colors,
    text=[f"{v:.1f}万円" for v in monthly_plot],
    textposition="inside",
    textfont=dict(size=10, color="white"),
    hovertemplate="<b>%{x}から受給</b><br>月々の年金手取り: %{y:.1f}万円<extra></extra>",
))

best_y_m = get_monthly_pension(next(r for r in all_results if r["start_age"] == best_age)) / 10000
fig_bar.update_layout(
    title="受給開始年齢ごとの月々の年金手取り額（受給開始直後）",
    xaxis_title="受給開始年齢",
    yaxis_title="月々の年金手取り額（万円／月）",
    yaxis=dict(range=[y_min * 0.92, y_range_top], gridcolor="#e9ecef"),
    plot_bgcolor="white",
    paper_bgcolor="white",
    font=dict(size=13),
    showlegend=False,
    height=460,
    annotations=[
        dict(
            x=f"{best_age}歳",
            y=best_y_m,
            yshift=52,
            text="🏆 最適",
            showarrow=True,
            arrowhead=2,
            arrowcolor="#1a3a5c",
            arrowsize=1.2,
            ax=0, ay=-36,
            font=dict(size=13, color="#1a3a5c", family="Arial Black"),
            bgcolor="#ffe066",
            borderpad=4,
            bordercolor="#1a3a5c",
            borderwidth=1.5,
        )
    ],
)
st.plotly_chart(fig_bar, use_container_width=True)


# ── 損益分岐点グラフ（累積手取り推移） ──
st.markdown("### 累積年金手取り額の推移（受給開始年齢別）")

fig_line = go.Figure()
highlight_ages = [60, 65, best_age, 70, 75]
highlight_ages = sorted(set(highlight_ages))

for r in all_results:
    sa = r["start_age"]
    df_r = r["df"]
    ages_x = df_r["age"].tolist()
    cumsum_net = (df_r["monthly_pension_gross"] * 12 * (1 - df_r.apply(
        lambda row: net_rate(row["annual_gross"]), axis=1
    ))).cumsum().tolist()

    if sa == best_age:
        fig_line.add_trace(go.Scatter(
            x=ages_x, y=[v / 10000 for v in cumsum_net],
            name=f"{sa}歳（最適）", line=dict(color="#ffe066", width=4),
            mode="lines"
        ))
    elif sa == 65:
        fig_line.add_trace(go.Scatter(
            x=ages_x, y=[v / 10000 for v in cumsum_net],
            name=f"{sa}歳（標準）", line=dict(color="#6fb3f5", width=2, dash="dot"),
            mode="lines"
        ))
    elif sa in [60, 70, 75]:
        fig_line.add_trace(go.Scatter(
            x=ages_x, y=[v / 10000 for v in cumsum_net],
            name=f"{sa}歳", line=dict(width=1.5, dash="dash"),
            mode="lines", opacity=0.6
        ))

fig_line.update_layout(
    xaxis_title="年齢", yaxis_title="年金累積手取り額（万円）",
    plot_bgcolor="white", paper_bgcolor="white",
    font=dict(size=12), height=380,
    legend=dict(orientation="h", yanchor="top", y=-0.15),
)
fig_line.update_yaxes(gridcolor="#e9ecef")
fig_line.update_xaxes(gridcolor="#e9ecef")
st.plotly_chart(fig_line, use_container_width=True)

st.divider()


# ═══════════════════════════════════════
# [3] 詳細テーブル
# ═══════════════════════════════════════
st.markdown("## 📋 全受給パターン詳細比較")

table_rows = []
for r in all_results:
    sa = r["start_age"]
    df_r = r["df"]
    # 受給開始後の平均月額（年金のみ）
    recv_df = df_r[df_r["receiving"]]
    avg_monthly_pension = recv_df["monthly_pension_gross"].mean() if len(recv_df) > 0 else 0
    # 加給年金総額
    total_kakyuu = r["lifetime_kakyuu"]
    # 在職老齢年金カット総額
    total_cut = r["total_zairou_cut"]
    # 増減率
    adj = r["adjustment_rate"] * 100
    adj_str = f"+{adj:.1f}%" if adj > 0 else (f"{adj:.1f}%" if adj < 0 else "±0%")
    # vs 65歳
    r65_net = next(x["lifetime_net"] for x in all_results if x["start_age"] == 65)
    diff = r["lifetime_net"] - r65_net

    monthly_net = get_monthly_pension(r)
    monthly_net_vs65 = monthly_net - monthly_65
    table_rows.append({
        "受給開始": f"{sa}歳",
        "増減率": adj_str,
        "月々の年金手取り": f"{monthly_net/10000:.1f}万円",
        "65歳比（月額）": (f"+{monthly_net_vs65/10000:.1f}万" if monthly_net_vs65 > 0 else f"{monthly_net_vs65/10000:.1f}万"),
        "在職老齢年金カット累計": f"{total_cut/10000:.0f}万円" if total_cut > 0 else "−",
        "加給年金受取累計": f"{total_kakyuu/10000:.0f}万円" if total_kakyuu > 0 else "−",
        "65歳比（総額）": (f"+{diff/10000:.0f}万" if diff > 0 else f"{diff/10000:.0f}万"),
        "最適": "🏆" if sa == best_age else "",
    })

df_table = pd.DataFrame(table_rows)
# スタイリング
def highlight_best(row):
    if row["最適"] == "🏆":
        return ["background-color: #fff9cc; font-weight: bold"] * len(row)
    elif row["受給開始"] == "65歳":
        return ["background-color: #e8f4fd"] * len(row)
    return [""] * len(row)

styled = df_table.style.apply(highlight_best, axis=1)
st.dataframe(styled, use_container_width=True, hide_index=True)

st.divider()

# ═══════════════════════════════════════
# PDFレポート出力
# ═══════════════════════════════════════
st.markdown("## 📄 PDFレポートをダウンロード")
st.caption("入力情報・最適提案・比較グラフ・詳細テーブルをまとめたレポートを出力します。")

if st.button("📥 PDFを生成する", type="secondary"):
    with st.spinner("PDFを生成中..."):
        try:
            pdf_bytes = generate_pdf(inp, all_results, best_age, best_net, diff_vs_65, reason_text)
            filename = f"年金受給戦略レポート_{datetime.date.today().strftime('%Y%m%d')}.pdf"
            st.download_button(
                label="⬇️ PDFをダウンロード",
                data=pdf_bytes,
                file_name=filename,
                mime="application/pdf",
                type="primary",
            )
            st.success("PDF生成完了！上のボタンからダウンロードしてください。")
        except Exception as e:
            st.error(f"PDF生成中にエラーが発生しました: {e}")

st.divider()


# ═══════════════════════════════════════
# [4] 年次詳細シミュレーション（最適 vs 65歳）
# ═══════════════════════════════════════
st.markdown("## 📅 年次詳細シミュレーション")

best_result = next(r for r in all_results if r["start_age"] == best_age)
result_65 = next(r for r in all_results if r["start_age"] == 65)


def build_display_df(result: dict, inp=inp) -> pd.DataFrame:
    df = result["df"].copy()
    df["年齢"] = df["age"].astype(str) + "歳"
    df["受給中"] = df["receiving"].map({True: "✅", False: "待機中"})
    df["基礎年金(月)"] = df["monthly_kiso"].map(lambda x: f"{x:,.0f}円" if x > 0 else "−")
    df["厚生年金(月)"] = df["monthly_kosei"].map(lambda x: f"{x:,.0f}円" if x > 0 else "−")
    df["在職カット(月)"] = df["zairou_cut"].map(lambda x: f"▼{x:,.0f}円" if x > 0 else "−")
    df["加給年金(月)"] = df["monthly_kakyuu"].map(lambda x: f"{x:,.0f}円" if x > 0 else "−")
    df["給与(年)"] = df.apply(
        lambda row: f"{(row['monthly_salary']*12 + (inp.bonus_before65 if row['age'] < 65 else inp.bonus_after65))/10000:.0f}万円"
        if row["monthly_salary"] > 0 else "−", axis=1
    )
    df["年金受取（年・額面）"] = (df["monthly_pension_gross"] * 12).map(lambda x: f"{x/10000:.1f}万円" if x > 0 else "−")
    df["控除額（税・社保概算）"] = (df["annual_gross"] - df["annual_net"]).map(
        lambda x: f"▼{x/10000:.1f}万円" if x > 0 else "−"
    )
    df["年間手取り（年金＋給与）"] = df["annual_net"].map(lambda x: f"{x/10000:.0f}万円")
    return df[["年齢", "受給中", "基礎年金(月)", "厚生年金(月)",
               "在職カット(月)", "加給年金(月)", "給与(年)",
               "年金受取（年・額面）", "控除額（税・社保概算）", "年間手取り（年金＋給与）"]]


COLS = ["年齢", "受給中", "基礎年金(月)", "厚生年金(月)",
        "在職カット(月)", "加給年金(月)", "給与(月)", "年間手取り"]

def show_result_tab(result: dict, reference_pension=None):
    """結果1件分のメトリクス＋テーブルを描画する共通関数"""
    total_pension = (result["df"]["monthly_pension_gross"] * 12).sum()
    # 年金のみの手取り（給与を除いた純粋な年金手取り）
    df_r = result["df"]
    pension_net = (df_r["monthly_pension_gross"] * 12 * (1 - df_r.apply(
        lambda row: net_rate(row["annual_gross"]), axis=1))).sum()

    col_a, col_b, col_c, col_d = st.columns(4)
    col_a.metric(
        "年金の総受取額（額面）",
        f"{total_pension/10000:.0f}万円",
        delta=(f"{(total_pension - reference_pension)/10000:+.0f}万円（最適比）" if reference_pension is not None and total_pension != reference_pension else None),
        delta_color="inverse",
    )
    col_b.metric(
        "年金の総手取り（概算）",
        f"{pension_net/10000:.0f}万円",
        help="税・社会保険料控除後の年金受取累計（給与との合算で計算）",
    )
    col_c.metric("加給年金 受取累計", f"{result['lifetime_kakyuu']/10000:.0f}万円")
    col_d.metric("在職老齢年金カット累計", f"{result['total_zairou_cut']/10000:.0f}万円")
    st.dataframe(build_display_df(result), use_container_width=True, hide_index=True)


# ── ラベル定義 ──
label_best = f"🏆 最適：{best_age}歳開始（{best_result['adjustment_rate']*100:+.1f}%）"
label_65   = f"📌 65歳開始（基準）"
label_free = "🔍 年齢を選んで比較"

if best_age == 65:
    tab_65, tab_free = st.tabs([label_65, label_free])

    with tab_65:
        show_result_tab(result_65)

    with tab_free:
        free_age = st.select_slider(
            "受給開始年齢を選択",
            options=list(range(60, 76)),
            value=70,
            format_func=lambda a: f"{a}歳",
            key="free_age_slider",
        )
        free_result = next(r for r in all_results if r["start_age"] == free_age)
        show_result_tab(free_result, reference_net=result_65["lifetime_net"])

else:
    tab_best, tab_65, tab_free = st.tabs([label_best, label_65, label_free])

    with tab_best:
        show_result_tab(best_result)

    with tab_65:
        show_result_tab(result_65, reference_pension=(result_65["df"]["monthly_pension_gross"] * 12).sum())

    with tab_free:
        # 最適・65歳以外のデフォルト値を決める
        other_ages = [a for a in range(60, 76) if a not in (best_age, 65)]
        default_free = other_ages[len(other_ages) // 2] if other_ages else 60
        free_age = st.select_slider(
            "受給開始年齢を選択",
            options=list(range(60, 76)),
            value=default_free,
            format_func=lambda a: (
                f"{a}歳  🏆最適" if a == best_age else
                f"{a}歳  📌65歳基準" if a == 65 else
                f"{a}歳"
            ),
            key="free_age_slider",
        )
        free_result = next(r for r in all_results if r["start_age"] == free_age)
        show_result_tab(free_result, reference_pension=(result_65["df"]["monthly_pension_gross"] * 12).sum())


# ─────────────────────────────────────────
# フッター
# ─────────────────────────────────────────
st.markdown("""
---
<div style="font-size:0.8rem; color:#6c757d; text-align:center;">
⚠️ <strong>免責事項</strong>：本シミュレーターは概算値を提供するものであり、実際の年金額・税額は個人の状況により異なります。
正確な試算は日本年金機構（ねんきんネット）または社会保険労務士にご相談ください。<br>
在職老齢年金基準：2026年4月改正後（月65万円）を適用。加給年金：2024年度額（年397,500円）を使用。
</div>
""", unsafe_allow_html=True)
