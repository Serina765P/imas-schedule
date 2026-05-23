#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


API_BASE = os.getenv("IMAS_API_BASE", "https://cmsapi-frontend.idolmaster-official.jp/sitern/api/")
CMS_API_BASE = os.getenv("IMAS_CMS_API_BASE", "https://cmsapi-frontend.idolmaster-official.jp/sitern/api/")
API_KEY = os.getenv("IMAS_API_KEY", "")
SITE = os.getenv("IMAS_SITE", "jp")
IP = os.getenv("IMAS_IP", "idolmaster")

BRAND_ZH = {
    "IDOLMASTER": "765PRO ALLSTARS",
    "CINDERELLAGIRLS": "灰姑娘女孩",
    "MILLIONLIVE": "百万现场！",
    "SIDEM": "SideM",
    "SHINYCOLORS": "闪耀色彩",
    "GAKUEN": "学园偶像大师",
    "VA-LIV": "VA-LIV",
    "OTHER": "其他",
}

CATEGORY_ZH = {
    "ライブ・イベント": "演出活动",
    "コラボ・キャンペーン": "联动活动",
    "グッズ": "周边",
    "ラジオ": "广播",
    "配信番組": "直播节目",
    "ミュージック": "音乐",
    "ゲーム": "游戏",
    "アニメ": "动画",
    "ブック・コミック": "图书漫画",
    "展覧会": "展览",
    "舞台": "舞台剧",
    "その他": "其他",
}


def month_bounds(month: str) -> Tuple[dt.date, dt.date]:
    start = dt.datetime.strptime(month, "%Y-%m").date().replace(day=1)
    if start.month == 12:
        next_month = dt.date(start.year + 1, 1, 1)
    else:
        next_month = dt.date(start.year, start.month + 1, 1)
    return start, next_month - dt.timedelta(days=1)


def add_months(date_obj: dt.date, months: int) -> dt.date:
    idx = date_obj.month - 1 + months
    year = date_obj.year + idx // 12
    month = idx % 12 + 1
    return dt.date(year, month, 1)


def api_get(path: str, params: Dict[str, Any], timeout: int = 20, base: Optional[str] = None) -> Dict[str, Any]:
    use_base = base or API_BASE
    url = use_base.rstrip("/") + "/" + path.lstrip("/")
    headers = {"X-API-KEY": API_KEY} if API_KEY else None
    response = requests.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def get_cms_token() -> str:
    data = api_get("cmsbase/Token/get", {"site": SITE, "ip": IP}, base=CMS_API_BASE)
    token = data.get("data", {}).get("token")
    if not token:
        raise RuntimeError("Failed to get cms token")
    return token


def fetch_schedule_range(start_date: dt.date, end_date: dt.date) -> Dict[str, Any]:
    token = get_cms_token()
    payload = {
        "category": ["SCHEDULE"],
        "target_start_date": start_date.strftime("%Y-%m-%d"),
        "target_end_date": end_date.strftime("%Y-%m-%d"),
    }
    params = {
        "site": SITE,
        "ip": IP,
        "token": token,
        "sort": "asc",
        "limit": 200,
        "data": json.dumps(payload, ensure_ascii=False),
    }
    return api_get("idolmaster/Article/list", params)


def fetch_schedule_window(center_month: str, months_before: int = 12, months_after: int = 12) -> Dict[str, Any]:
    center = dt.datetime.strptime(center_month, "%Y-%m").date().replace(day=1)
    start_month = add_months(center, -months_before)
    end_month = add_months(center, months_after)
    merged: Dict[str, Dict[str, Any]] = {}
    months: List[str] = []

    cursor = start_month
    while cursor <= end_month:
        month_key = cursor.strftime("%Y-%m")
        months.append(month_key)
        month_start, month_end = month_bounds(month_key)
        raw = fetch_schedule_range(month_start, month_end)
        for item in raw.get("data", {}).get("article_list", []) or []:
            item_id = str(item.get("_id") or item.get("path") or len(merged))
            merged[item_id] = item
        cursor = add_months(cursor, 1)

    article_list = sorted(
        merged.values(),
        key=lambda x: (x.get("event_startdate") or 0, str(x.get("title") or "")),
    )
    return {
        "statusCode": 200,
        "window": {
            "center_month": center_month,
            "months": months,
            "start_date": start_month.isoformat(),
            "end_date": month_bounds(end_month.strftime("%Y-%m"))[1].isoformat(),
        },
        "data": {"apiStatus": True, "count": len(article_list), "article_list": article_list},
    }


def to_iso_date(unix_ts: Optional[int]) -> Optional[str]:
    if not unix_ts:
        return None
    return dt.datetime.fromtimestamp(int(unix_ts), dt.UTC).date().isoformat()


def infer_brand_codes(article: Dict[str, Any], brand_codes: List[str]) -> List[str]:
    joined = " ".join(str(article.get(key) or "") for key in ["title", "event_url", "url", "path", "content"]).lower()
    has_valiv_hint = "va-liv" in joined or "valiv" in joined or "ヴイアライヴ" in joined
    if has_valiv_hint:
        brand_codes = ["VA-LIV" if code == "OTHER" else code for code in brand_codes]
        if not brand_codes:
            brand_codes = ["VA-LIV"]
    if len(brand_codes) > 1:
        brand_codes = [code for code in brand_codes if code != "OTHER"]
    return brand_codes or ["OTHER"]


def normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    for article in raw.get("data", {}).get("article_list", []) or []:
        brand_names_ja: List[str] = []
        brand_codes: List[str] = []
        brand_raw = article.get("brand")
        if isinstance(brand_raw, list):
            for brand in brand_raw:
                if isinstance(brand, dict):
                    if isinstance(brand.get("name"), str):
                        brand_names_ja.append(brand["name"])
                    if isinstance(brand.get("code"), str):
                        brand_codes.append(brand["code"])
        elif isinstance(brand_raw, dict):
            if isinstance(brand_raw.get("name"), str):
                brand_names_ja.append(brand_raw["name"])
            if isinstance(brand_raw.get("code"), str):
                brand_codes.append(brand_raw["code"])

        brand_codes = infer_brand_codes(article, brand_codes)
        brand_names_zh = [BRAND_ZH.get(code, code) for code in brand_codes]

        category_names_ja: List[str] = []
        categories = article.get("categories")
        if isinstance(categories, dict) and isinstance(categories.get("subcategory"), list):
            for category in categories["subcategory"]:
                if isinstance(category, dict) and isinstance(category.get("name"), str):
                    category_names_ja.append(category["name"])
        category_names_zh = [CATEGORY_ZH.get(name, name) for name in category_names_ja]

        items.append(
            {
                "id": article.get("_id"),
                "title": (article.get("title") or article.get("event_title") or "(no title)").strip(),
                "brand": " / ".join(brand_names_zh),
                "brand_ja": " / ".join(brand_names_ja),
                "brand_codes": brand_codes,
                "brand_list": brand_names_zh,
                "category": " / ".join(category_names_zh),
                "category_ja": " / ".join(category_names_ja),
                "category_list": category_names_zh,
                "start_date": to_iso_date(article.get("event_startdate")),
                "end_date": to_iso_date(article.get("event_enddate")),
                "display_date": article.get("event_dspdate"),
                "url": article.get("event_url") or article.get("path"),
            }
        )

    items.sort(key=lambda x: (x.get("start_date") or "9999-99-99", x.get("title") or ""))
    return {"count": len(items), "items": items, "window": raw.get("window", {})}


def item_touches_month(item: Dict[str, Any], month: str) -> bool:
    start, end = month_bounds(month)
    item_start = item.get("start_date")
    item_end = item.get("end_date")
    if not item_start or not item_end:
        return False
    return item_start <= end.isoformat() and item_end >= start.isoformat()


def build_month_buckets(normalized: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    months = normalized.get("window", {}).get("months", [])
    all_items = normalized.get("items", [])
    buckets: Dict[str, Dict[str, Any]] = {}
    for month in months:
        month_items = [item for item in all_items if item_touches_month(item, month)]
        buckets[month] = {"month": month, "count": len(month_items), "items": month_items}
    return buckets


def build_html() -> str:
    return '''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>偶像大师日程表</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;700;800&family=Noto+Sans+JP:wght@400;500;700;800&family=Outfit:wght@700;800&display=swap" rel="stylesheet">
  <script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>
  <style>
    :root { --bg:#edf2fa; --bg2:#fbfdff; --ink:#394356; --muted:#72809b; --line:#d9e1ee; --line2:#c8d2e4; --pink:#f57fba; --pink2:#ffa4d1; --navy:#4d5770; --panel:#ffffffdb; --chip:#f3f6fb; --shadow:0 14px 32px rgba(81,99,140,.09); }
    * { box-sizing:border-box; }
    body { margin:0; font-family:"Noto Sans SC","Noto Sans JP",sans-serif; color:var(--ink); background:
      radial-gradient(1200px 700px at -10% 10%, rgba(255,255,255,.98), transparent 60%),
      radial-gradient(700px 400px at 110% 80%, rgba(190,255,231,.33), transparent 40%),
      linear-gradient(160deg, var(--bg2), var(--bg)); }
    body::before { content:""; position:fixed; inset:0; pointer-events:none; background:
      linear-gradient(120deg, transparent 0 18%, rgba(255,255,255,.5) 18% 20%, transparent 20% 100%),
      radial-gradient(circle at 82% 16%, rgba(255,255,255,.8), transparent 10%),
      linear-gradient(90deg, transparent 0 88%, rgba(255,255,255,.32) 88% 89%, transparent 89% 100%); opacity:.82; }
    .wrap { max-width:1460px; margin:0 auto; padding:18px 14px 42px; position:relative; }
    .hero { text-align:center; margin:12px 0 18px; }
    .hero-title { margin:0; font:800 62px/1 "Outfit","Noto Sans SC",sans-serif; letter-spacing:.08em; color:var(--navy); }
    .hero-subtitle { margin-top:6px; color:var(--muted); font-size:14px; }
    .month-switch { display:flex; justify-content:center; align-items:center; gap:12px; margin-top:14px; }
    .month-btn { width:40px; height:40px; border:none; border-radius:999px; background:#fff; color:var(--pink); box-shadow:0 8px 18px rgba(81,99,140,.12); font-size:20px; cursor:pointer; }
    .month-label { color:var(--navy); font-size:30px; font-weight:800; }
    .shell { display:grid; grid-template-columns:260px minmax(0,1fr) 250px; gap:18px; align-items:start; }
    .panel { background:var(--panel); border:1px solid rgba(255,255,255,.76); border-radius:24px; backdrop-filter:blur(8px); box-shadow:var(--shadow); }
    .left,.right { position:sticky; top:16px; display:grid; gap:14px; }
    .card-pad { padding:14px; }
    .muted { color:var(--muted); font-size:13px; }
    .section-title { margin:0 0 10px; color:var(--navy); font-size:14px; font-weight:800; }
    .stats { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:12px; }
    .stat { border:1px solid var(--line); border-radius:16px; padding:10px 12px; background:#fff; }
    .stat-label { color:var(--muted); font-size:12px; }
    .stat-value { margin-top:4px; color:var(--navy); font-size:24px; font-weight:800; }
    .search-box,.select { width:100%; border:1px solid var(--line); border-radius:14px; background:#fff; color:var(--ink); padding:11px 12px; font:inherit; }
    .stack { display:grid; gap:10px; }
    .toggle-row { display:flex; gap:8px; flex-wrap:wrap; }
    .toggle { border:none; border-radius:999px; padding:9px 12px; background:var(--chip); color:var(--navy); font:inherit; font-weight:700; cursor:pointer; }
    .toggle.active { background:linear-gradient(120deg, var(--pink), var(--pink2)); color:#fff; }
    .chip-grid,.anchor-grid { display:grid; gap:8px; margin-top:10px; }
    .chip-item,.anchor-item { display:flex; align-items:center; gap:10px; border:1px solid var(--line); background:#fff; border-radius:14px; padding:10px 12px; cursor:pointer; text-align:left; }
    .chip-item.active,.anchor-item.active { border-color:var(--line2); background:linear-gradient(180deg,#fff,#fff7fb); box-shadow:inset 0 0 0 1px rgba(245,127,186,.34); }
    .chip-item img { height:18px; width:auto; object-fit:contain; }
    .chip-item span,.anchor-item span { flex:1; color:var(--navy); font-size:14px; }
    .anchor-item small { color:var(--muted); }
    .calendar { padding:14px; }
    .calendar-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; color:var(--navy); font-weight:800; }
    .calendar-grid { display:grid; grid-template-columns:repeat(7,1fr); gap:6px; }
    .dow { text-align:center; color:var(--muted); font-size:12px; font-weight:700; padding:2px 0; }
    .day { aspect-ratio:1 / 1; width:100%; border:none; border-radius:999px; background:#fff; color:var(--navy); padding:0; cursor:pointer; font:inherit; box-shadow:inset 0 0 0 1px rgba(217,225,238,.9); display:grid; place-items:center; }
    .day.empty { background:transparent; box-shadow:none; cursor:default; }
    .day-number { font-size:13px; font-weight:800; text-align:center; }
    .content { display:grid; gap:18px; }
    .section { padding:14px; }
    .section-header { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:10px; }
    .section-left { display:flex; align-items:center; gap:10px; }
    .section-heading { margin:0; color:var(--navy); font-size:24px; font-weight:800; }
    .collapse-btn { border:none; border-radius:999px; background:var(--chip); color:var(--navy); padding:8px 12px; font:inherit; font-weight:700; cursor:pointer; }
    .event-list { display:grid; gap:10px; }
    .group + .group { margin-top:18px; }
    .group { scroll-margin-top:16px; }
    .group.flash { animation:flashPulse 1.2s ease; }
    .group-header { display:flex; justify-content:space-between; align-items:end; margin:4px 2px 10px; }
    .group-title { color:var(--navy); font-size:22px; font-weight:800; }
    .group-sub { color:var(--muted); font-size:13px; }
    .event-card { display:grid; grid-template-columns:112px 1fr 62px; border:1px solid var(--line); background:#fff; border-radius:20px; overflow:hidden; }
    .event-state { display:flex; align-items:center; justify-content:center; padding:8px; color:var(--navy); font-size:14px; font-weight:800; background:linear-gradient(180deg,#f7f9fc,#f0f4fb); border-right:1px solid var(--line); text-align:center; }
    .event-state.ongoing { background:linear-gradient(180deg,var(--pink2),var(--pink)); color:#fff; }
    .event-state.upcoming { background:linear-gradient(180deg,#dff4ff,#eff8ff); }
    .event-state.past { background:linear-gradient(180deg,#f5f7fb,#eef2f8); }
    .event-main { padding:14px 14px 12px; }
    .event-top { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:8px; }
    .tag { background:#909bb5; color:#fff; border-radius:999px; padding:4px 10px; font-size:12px; font-weight:800; }
    .brand-text { color:#5a6884; font-size:13px; font-weight:700; }
    .event-title { margin:0 0 10px; color:#3f475c; font-size:18px; font-weight:700; line-height:1.45; }
    .event-meta { display:flex; flex-wrap:wrap; gap:12px; align-items:center; margin-bottom:8px; }
    .period { color:var(--navy); font-size:14px; font-weight:800; }
    .time { color:var(--muted); font-size:13px; }
    .logos { display:flex; flex-wrap:wrap; gap:8px; align-items:center; min-height:22px; margin-top:2px; }
    .logos img { height:18px; width:auto; object-fit:contain; opacity:.98; filter:drop-shadow(0 1px 0 rgba(255,255,255,.8)); }
    .go { display:flex; align-items:center; justify-content:center; border:none; border-left:1px solid var(--line); background:linear-gradient(180deg,#fff,#fff7fb); cursor:pointer; }
    .go .bubble { width:40px; height:40px; display:grid; place-items:center; border-radius:999px; background:linear-gradient(135deg,var(--pink2),var(--pink)); color:#fff; font-size:18px; box-shadow:0 8px 18px rgba(245,127,186,.28); transition:transform .15s ease; }
    .go:hover .bubble { transform:translateX(2px); }
    .empty-card,.loading-card { padding:26px; border:1px dashed var(--line2); border-radius:18px; text-align:center; color:var(--muted); background:#fff; }
    .fab-top { position:fixed; right:18px; bottom:18px; width:52px; height:52px; border:none; border-radius:999px; background:linear-gradient(135deg,var(--pink2),var(--pink)); color:#fff; box-shadow:0 12px 24px rgba(245,127,186,.32); font-size:20px; cursor:pointer; z-index:40; }
    .filter-overlay { position:fixed; inset:0; background:rgba(0,0,0,.32); z-index:100; }
    .filter-drawer { position:fixed; top:0; left:0; bottom:0; width:280px; max-width:86vw; background:var(--panel); backdrop-filter:blur(12px); z-index:101; overflow-y:auto; padding:16px; box-shadow:4px 0 24px rgba(0,0,0,.12); }
    .filter-drawer-enter-active,.filter-drawer-leave-active { transition:transform .28s ease; }
    .filter-drawer-enter-from,.filter-drawer-leave-to { transform:translateX(-100%); }
    .filter-drawer-enter-to,.filter-drawer-leave-from { transform:translateX(0); }
    .filter-drawer .close-btn { float:right; border:none; border-radius:999px; background:var(--chip); color:var(--navy); width:36px; height:36px; font-size:18px; cursor:pointer; display:grid; place-items:center; }
    .filter-fab { position:fixed; left:12px; bottom:18px; width:52px; height:52px; border:none; border-radius:999px; background:linear-gradient(135deg,var(--pink2),var(--pink)); color:#fff; box-shadow:0 12px 24px rgba(245,127,186,.32); font-size:20px; cursor:pointer; z-index:40; display:none; place-items:center; font-weight:700; }
    .filter-fab .badge { position:absolute; top:-4px; right:-4px; min-width:20px; height:20px; border-radius:999px; background:var(--navy); color:#fff; font-size:11px; padding:0 5px; display:grid; place-items:center; }
    .fade-slide-enter-active,.fade-slide-leave-active { transition:opacity .25s ease, transform .25s ease; }
    .fade-slide-enter-from,.fade-slide-leave-to { opacity:0; transform:translateY(8px); }
    .section-expand-enter-active,.section-expand-leave-active { transition:max-height .28s ease, opacity .24s ease, transform .24s ease; overflow:hidden; }
    .section-expand-enter-from,.section-expand-leave-to { max-height:0; opacity:0; transform:translateY(-6px); }
    .section-expand-enter-to,.section-expand-leave-from { max-height:4000px; opacity:1; transform:translateY(0); }
    .card-stagger-enter-active { transition:opacity .35s ease, transform .35s ease; }
    .card-stagger-enter-from { opacity:0; transform:translateY(10px); }
    @keyframes flashPulse { 0% { box-shadow:0 0 0 0 rgba(245,127,186,0); } 25% { box-shadow:0 0 0 6px rgba(245,127,186,.14); } 100% { box-shadow:0 0 0 0 rgba(245,127,186,0); } }
    @media (max-width:1180px) { .shell { grid-template-columns:1fr; } .left,.right { position:static; } .right { order:1; } .content { order:2; } .left { order:3; } }
    @media (max-width:760px) { .wrap { padding:14px 10px 88px; } .hero-title { font-size:38px; } .hero-subtitle { font-size:12px; } .month-label { font-size:24px; } .month-switch { gap:8px; margin-top:10px; } .month-btn { width:44px; height:44px; } .panel { border-radius:20px; } .card-pad,.section,.calendar { padding:12px; } .section-header { align-items:flex-start; } .chip-grid { grid-template-columns:1fr 1fr; } .chip-item { min-height:46px; } .left { display:none; } .filter-fab { display:grid; } .right { position:static; top:auto; z-index:auto; } .calendar { box-shadow:0 10px 24px rgba(81,99,140,.12); } .calendar-grid { gap:5px; } .event-card { grid-template-columns:1fr; } .event-state { border-right:none; border-bottom:1px solid var(--line); padding:10px 12px; justify-content:flex-start; } .event-main { padding:12px; } .event-title { font-size:16px; } .event-meta { gap:8px; } .go { border-left:none; border-top:1px solid var(--line); min-height:54px; } .go .bubble { width:42px; height:42px; } .logos img { height:20px; } }
  </style>
</head>
<body>
  <main id="app" class="wrap">
    <header class="hero">
      <h1 class="hero-title">iM@S SCHEDULE</h1>
      <div class="hero-subtitle">偶像大师日程 · 浏览区间 {{ windowText }}</div>
      <div class="month-switch">
        <button class="month-btn" @click="stepMonth(-1)">‹</button>
        <div class="month-label">{{ monthTitle }}</div>
        <button class="month-btn" @click="stepMonth(1)">›</button>
      </div>
    </header>

    <section class="shell" v-if="ready">
      <aside class="left">
        <div class="panel card-pad">
          <div class="stats">
            <div class="stat"><div class="stat-label">当前结果</div><div class="stat-value">{{ filteredCount }}</div></div>
            <div class="stat"><div class="stat-label">进行中</div><div class="stat-value">{{ ongoingItems.length }}</div></div>
          </div>
        </div>

        <div class="panel card-pad">
          <h2 class="section-title">搜索与排序</h2>
          <div class="stack">
            <input class="search-box" v-model.trim="query" placeholder="搜索标题、品牌或分类" />
            <select class="select" v-model="sortBy">
              <option value="start_asc">按开始时间升序</option>
              <option value="start_desc">按开始时间降序</option>
              <option value="title_asc">按标题排序</option>
            </select>
            <div class="toggle-row">
              <button class="toggle" :class="{ active: onlyOngoing }" @click="onlyOngoing = !onlyOngoing">仅看进行中</button>
            </div>
          </div>
        </div>

        <div class="panel card-pad">
          <h2 class="section-title">企划筛选</h2>
          <div class="chip-grid">
            <button class="chip-item" :class="{ active: selectedBrands.length === 0 }" @click="selectedBrands = []"><span>全部企划</span></button>
            <button v-for="brand in brandOptions" :key="brand.code" class="chip-item" :class="{ active: selectedBrands.includes(brand.code) }" @click="toggleBrand(brand.code)">
              <img :src="brand.logo" :alt="brand.name" />
              <span>{{ brand.name }}</span>
            </button>
          </div>
        </div>

        <div class="panel card-pad">
          <h2 class="section-title">活动类别筛选</h2>
          <div class="chip-grid">
            <button class="chip-item" :class="{ active: selectedCategories.length === 0 }" @click="selectedCategories = []"><span>全部类别</span></button>
            <button v-for="category in categoryOptions" :key="category" class="chip-item" :class="{ active: selectedCategories.includes(category) }" @click="toggleCategory(category)"><span>{{ category }}</span></button>
          </div>
        </div>
      </aside>

      <transition name="filter-drawer">
      <div v-if="showFilterDrawer" class="filter-overlay" @click="showFilterDrawer = false"></div>
      </transition>
      <transition name="filter-drawer">
      <aside v-if="showFilterDrawer" class="filter-drawer">
        <button class="close-btn" @click="showFilterDrawer = false">✕</button>
        <div class="panel card-pad" style="margin-bottom:14px">
          <div class="stats">
            <div class="stat"><div class="stat-label">当前结果</div><div class="stat-value">{{ filteredCount }}</div></div>
            <div class="stat"><div class="stat-label">进行中</div><div class="stat-value">{{ ongoingItems.length }}</div></div>
          </div>
        </div>
        <div class="panel card-pad" style="margin-bottom:14px">
          <h2 class="section-title">搜索与排序</h2>
          <div class="stack">
            <input class="search-box" v-model.trim="query" placeholder="搜索标题、品牌或分类" />
            <select class="select" v-model="sortBy">
              <option value="start_asc">按开始时间升序</option>
              <option value="start_desc">按开始时间降序</option>
              <option value="title_asc">按标题排序</option>
            </select>
            <div class="toggle-row">
              <button class="toggle" :class="{ active: onlyOngoing }" @click="onlyOngoing = !onlyOngoing">仅看进行中</button>
            </div>
          </div>
        </div>
        <div class="panel card-pad" style="margin-bottom:14px">
          <h2 class="section-title">企划筛选</h2>
          <div class="chip-grid">
            <button class="chip-item" :class="{ active: selectedBrands.length === 0 }" @click="selectedBrands = []"><span>全部企划</span></button>
            <button v-for="brand in brandOptions" :key="brand.code" class="chip-item" :class="{ active: selectedBrands.includes(brand.code) }" @click="toggleBrand(brand.code)">
              <img :src="brand.logo" :alt="brand.name" />
              <span>{{ brand.name }}</span>
            </button>
          </div>
        </div>
        <div class="panel card-pad">
          <h2 class="section-title">活动类别筛选</h2>
          <div class="chip-grid">
            <button class="chip-item" :class="{ active: selectedCategories.length === 0 }" @click="selectedCategories = []"><span>全部类别</span></button>
            <button v-for="category in categoryOptions" :key="category" class="chip-item" :class="{ active: selectedCategories.includes(category) }" @click="toggleCategory(category)"><span>{{ category }}</span></button>
          </div>
        </div>
      </aside>
      </transition>

      <div class="content">
        <transition name="fade-slide" mode="out-in">
        <section class="panel section" id="section-ongoing" :key="'ongoing-' + currentMonth">
          <div class="section-header">
            <div class="section-left"><h2 class="section-heading">正在进行</h2><span class="muted">{{ ongoingItems.length }} 条</span></div>
            <button class="collapse-btn" @click="collapsed.ongoing = !collapsed.ongoing">{{ collapsed.ongoing ? '展开' : '折叠' }}</button>
          </div>
          <transition name="section-expand">
          <div v-if="!collapsed.ongoing">
            <transition-group name="card-stagger" tag="div" class="event-list" v-if="ongoingItems.length">
              <article class="event-card" v-for="e in ongoingItems" :key="'ongoing-' + e.id">
                <div class="event-state ongoing">正在进行</div>
                <div class="event-main">
                  <div class="event-top"><span class="tag" v-for="c in e.category_list" :key="e.id + '-oc-' + c">{{ c }}</span><span class="brand-text">{{ e.brand }}</span></div>
                  <p class="event-title">{{ e.title }}</p>
                  <div class="event-meta"><span class="period">时间范围：{{ e.start_date }} → {{ e.end_date }}</span><span class="time">官方标注：{{ e.display_date || '未标注' }}</span></div>
                  <div class="logos"><template v-for="code in e.brand_codes" :key="e.id + '-ob-' + code"><img v-if="logoFor(code)" :src="logoFor(code)" :alt="brandName(code)" :title="brandName(code)" /></template></div>
                </div>
                <button class="go" @click="openLink(e.url)"><span class="bubble">↗</span></button>
              </article>
            </transition-group>
            <div class="empty-card" v-else>当前筛选条件下没有正在进行的事件。</div>
          </div>
          </transition>
        </section>
        </transition>

        <transition name="fade-slide" mode="out-in">
        <section class="panel section" id="section-upcoming" :key="'upcoming-' + currentMonth">
          <div class="section-header">
            <div class="section-left"><h2 class="section-heading">即将开始</h2><span class="muted">{{ upcomingCount }} 条</span></div>
            <button class="collapse-btn" @click="collapsed.upcoming = !collapsed.upcoming">{{ collapsed.upcoming ? '展开' : '折叠' }}</button>
          </div>
          <transition name="section-expand">
          <div v-if="!collapsed.upcoming">
            <template v-if="upcomingGroups.length">
              <section class="group" :class="{ flash: activeFlashAnchor === group.anchor }" v-for="group in upcomingGroups" :key="group.anchor" :id="group.anchor">
                <div class="group-header"><div class="group-title">{{ group.label }}</div><div class="group-sub">{{ group.items.length }} 条</div></div>
                <transition-group name="card-stagger" tag="div" class="event-list">
                  <article class="event-card" v-for="e in group.items" :key="e.id">
                    <div class="event-state upcoming">即将开始</div>
                    <div class="event-main">
                      <div class="event-top"><span class="tag" v-for="c in e.category_list" :key="e.id + '-uc-' + c">{{ c }}</span><span class="brand-text">{{ e.brand }}</span></div>
                      <p class="event-title">{{ e.title }}</p>
                      <div class="event-meta"><span class="period">时间范围：{{ e.start_date }} → {{ e.end_date }}</span><span class="time">官方标注：{{ e.display_date || '未标注' }}</span></div>
                      <div class="logos"><template v-for="code in e.brand_codes" :key="e.id + '-ub-' + code"><img v-if="logoFor(code)" :src="logoFor(code)" :alt="brandName(code)" :title="brandName(code)" /></template></div>
                    </div>
                    <button class="go" @click="openLink(e.url)"><span class="bubble">↗</span></button>
                  </article>
                </transition-group>
              </section>
            </template>
            <div class="empty-card" v-else>当前筛选条件下没有即将开始的事件。</div>
          </div>
          </transition>
        </section>
        </transition>

        <transition name="fade-slide" mode="out-in">
        <section class="panel section" id="section-past" :key="'past-' + currentMonth">
          <div class="section-header">
            <div class="section-left"><h2 class="section-heading">已经结束</h2><span class="muted">{{ pastCount }} 条</span></div>
            <button class="collapse-btn" @click="collapsed.past = !collapsed.past">{{ collapsed.past ? '展开' : '折叠' }}</button>
          </div>
          <transition name="section-expand">
          <div v-if="!collapsed.past">
            <template v-if="pastGroups.length">
              <section class="group" :class="{ flash: activeFlashAnchor === group.anchor }" v-for="group in pastGroups" :key="group.anchor" :id="group.anchor">
                <div class="group-header"><div class="group-title">{{ group.label }}</div><div class="group-sub">{{ group.items.length }} 条</div></div>
                <transition-group name="card-stagger" tag="div" class="event-list">
                  <article class="event-card" v-for="e in group.items" :key="e.id">
                    <div class="event-state past">已经结束</div>
                    <div class="event-main">
                      <div class="event-top"><span class="tag" v-for="c in e.category_list" :key="e.id + '-pc-' + c">{{ c }}</span><span class="brand-text">{{ e.brand }}</span></div>
                      <p class="event-title">{{ e.title }}</p>
                      <div class="event-meta"><span class="period">时间范围：{{ e.start_date }} → {{ e.end_date }}</span><span class="time">官方标注：{{ e.display_date || '未标注' }}</span></div>
                      <div class="logos"><template v-for="code in e.brand_codes" :key="e.id + '-pb-' + code"><img v-if="logoFor(code)" :src="logoFor(code)" :alt="brandName(code)" :title="brandName(code)" /></template></div>
                    </div>
                    <button class="go" @click="openLink(e.url)"><span class="bubble">↗</span></button>
                  </article>
                </transition-group>
              </section>
            </template>
            <div class="empty-card" v-else>当前筛选条件下没有已经结束的事件。</div>
          </div>
          </transition>
        </section>
        </transition>
      </div>

      <aside class="right">
        <div class="panel calendar">
          <div class="calendar-head"><button class="month-btn" @click="stepMonth(-1)">‹</button><span>{{ monthTitle }}</span><button class="month-btn" @click="stepMonth(1)">›</button></div>
          <div class="calendar-grid">
            <div class="dow" v-for="d in ['日','一','二','三','四','五','六']" :key="d">{{ d }}</div>
            <button v-for="cell in calendarCells" :key="cell.key" class="day" :class="{ empty: !cell.date }" :style="cell.style" @click="jumpTo(cell.anchor)">
              <div class="day-number">{{ cell.day || '' }}</div>
            </button>
          </div>
        </div>
      </aside>
    </section>

    <section v-else class="loading-card">正在加载日程数据...</section>
    <button v-if="ready" class="fab-top" @click="scrollTop">↑</button>
    <button v-if="ready" class="filter-fab" @click="showFilterDrawer = !showFilterDrawer">☰<span v-if="activeFilterCount" class="badge">{{ activeFilterCount }}</span></button>
  </main>

  <script>
    const BRAND_META = {
      IDOLMASTER: { name:'765PRO ALLSTARS', logo:'assets/logos/765as.svg' },
      CINDERELLAGIRLS: { name:'灰姑娘女孩', logo:'assets/logos/cinderella.svg' },
      MILLIONLIVE: { name:'百万现场！', logo:'assets/logos/million.svg' },
      SIDEM: { name:'SideM', logo:'assets/logos/sidem.svg' },
      SHINYCOLORS: { name:'闪耀色彩', logo:'assets/logos/sc.svg' },
      GAKUEN: { name:'学园偶像大师', logo:'assets/logos/gaku.svg' },
      'VA-LIV': { name:'VA-LIV', logo:'assets/logos/valiv.svg' },
      OTHER: { name:'其他', logo:'' }
    };
    const WEEKDAY = ['周日','周一','周二','周三','周四','周五','周六'];

    Vue.createApp({
      data() {
        return {
          ready: false,
          manifest: null,
          monthData: { month: '', items: [] },
          monthCache: {},
          query: '',
          selectedBrands: [],
          selectedCategories: [],
          sortBy: 'start_asc',
          onlyOngoing: false,
          currentMonth: '',
          collapsed: { ongoing: false, upcoming: false, past: true },
          activeFlashAnchor: '',
          showFilterDrawer: false
        };
      },
      computed: {
        windowText() { return this.manifest ? `${this.manifest.start_date} 至 ${this.manifest.end_date}` : ''; },
        monthTitle() { if (!this.currentMonth) return ''; const [y,m] = this.currentMonth.split('-'); return `${y} 年 ${Number(m)} 月`; },
        availableMonths() { return this.manifest?.months || []; },
        brandOptions() { return Object.entries(BRAND_META).filter(([code]) => code !== 'OTHER').map(([code, meta]) => ({ code, name: meta.name, logo: meta.logo })); },
        categoryOptions() { return Array.from(new Set((this.monthData.items || []).flatMap(item => item.category_list || []))).sort((a,b) => a.localeCompare(b, 'zh')); },
        preparedItems() {
          const today = new Date().toISOString().slice(0, 10);
          return (this.monthData.items || []).map(item => {
            const status = item.start_date <= today && item.end_date >= today ? 'ongoing' : (item.start_date > today ? 'upcoming' : 'past');
            return { ...item, status };
          });
        },
        filteredBase() {
          const q = this.query.toLowerCase();
          const items = this.preparedItems.filter(item => {
            if (this.selectedBrands.length && !(item.brand_codes || []).some(code => this.selectedBrands.includes(code))) return false;
            if (this.selectedCategories.length && !(item.category_list || []).some(name => this.selectedCategories.includes(name))) return false;
            if (this.onlyOngoing && item.status !== 'ongoing') return false;
            if (!q) return true;
            const haystack = [item.title, item.brand, item.category].filter(Boolean).join(' ').toLowerCase();
            return haystack.includes(q);
          });
          if (this.sortBy === 'start_asc') items.sort((a,b) => String(a.start_date).localeCompare(String(b.start_date)) || String(a.title).localeCompare(String(b.title), 'ja'));
          if (this.sortBy === 'start_desc') items.sort((a,b) => String(b.start_date).localeCompare(String(a.start_date)) || String(a.title).localeCompare(String(b.title), 'ja'));
          if (this.sortBy === 'title_asc') items.sort((a,b) => String(a.title).localeCompare(String(b.title), 'ja'));
          return items;
        },
        ongoingItems() { return this.filteredBase.filter(item => item.status === 'ongoing'); },
        upcomingItems() { return this.filteredBase.filter(item => item.status === 'upcoming'); },
        pastItems() { return this.filteredBase.filter(item => item.status === 'past'); },
        upcomingGroups() { return this.groupByDate(this.upcomingItems, 'upcoming', false); },
        pastGroups() { return this.groupByDate(this.pastItems, 'past', true); },
        filteredCount() { return this.filteredBase.length; },
        upcomingCount() { return this.upcomingItems.length; },
        pastCount() { return this.pastItems.length; },
        activeFilterCount() { return (this.selectedBrands.length > 0 ? 1 : 0) + (this.selectedCategories.length > 0 ? 1 : 0) + (this.onlyOngoing ? 1 : 0) + (this.query ? 1 : 0); },
        dayMap() {
          const result = {};
          [...this.upcomingGroups, ...this.pastGroups].forEach(group => { result[group.date] = { count: group.items.length, anchor: group.anchor }; });
          return result;
        },
        calendarCells() {
          if (!this.currentMonth) return [];
          const [y, m] = this.currentMonth.split('-').map(Number);
          const first = new Date(y, m - 1, 1);
          const last = new Date(y, m, 0);
          const cells = [];
          const map = this.dayMap;
          const maxCount = Math.max(...Object.values(map).map(v => v.count), 0);
          for (let i = 0; i < first.getDay(); i++) cells.push({ key: 'b' + i, day: '', date: '', count: 0, anchor: '', style: '' });
          for (let d = 1; d <= last.getDate(); d++) {
            const date = `${y}-${String(m).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
            const info = map[date] || { count: 0, anchor: '' };
            const alpha = maxCount ? 0.12 + (info.count / maxCount) * 0.58 : 0;
            const style = info.count ? `background: rgba(245,127,186,${alpha.toFixed(3)}); box-shadow: inset 0 0 0 1px rgba(245,127,186,.22);` : '';
            cells.push({ key: 'd' + d, day: d, date, count: info.count, anchor: info.anchor, style });
          }
          return cells;
        }
      },
      methods: {
        async init() {
          const manifestRes = await fetch('data/manifest.json');
          this.manifest = await manifestRes.json();
          this.currentMonth = this.manifest.center_month;
          await this.loadMonth(this.currentMonth);
          this.prefetchNeighbors(this.currentMonth);
          this.ready = true;
        },
        async fetchMonthData(month) {
          if (this.monthCache[month]) return this.monthCache[month];
          const res = await fetch(`data/months/${month}.json`);
          const payload = await res.json();
          this.monthCache[month] = payload;
          return payload;
        },
        async loadMonth(month) {
          this.monthData = await this.fetchMonthData(month);
        },
        prefetchNeighbors(month) {
          const idx = this.availableMonths.indexOf(month);
          if (idx < 0) return;
          [idx - 1, idx + 1].forEach(async nextIdx => {
            if (nextIdx >= 0 && nextIdx < this.availableMonths.length) {
              const target = this.availableMonths[nextIdx];
              if (!this.monthCache[target]) {
                try { await this.fetchMonthData(target); } catch (e) {}
              }
            }
          });
        },
        logoFor(code) { return BRAND_META[code]?.logo || ''; },
        brandName(code) { return BRAND_META[code]?.name || code; },
        toggleBrand(code) { this.selectedBrands = this.selectedBrands.includes(code) ? this.selectedBrands.filter(x => x !== code) : [...this.selectedBrands, code]; },
        toggleCategory(category) { this.selectedCategories = this.selectedCategories.includes(category) ? this.selectedCategories.filter(x => x !== category) : [...this.selectedCategories, category]; },
        openLink(url) { if (!url) return; const target = url.startsWith('/') ? 'https://idolmaster-official.jp' + url : url; window.open(target, '_blank', 'noopener'); },
        formatDateLabel(date) { if (!date || date === 'unknown') return '日期未标注'; const d = new Date(date + 'T00:00:00'); return `${d.getMonth() + 1}月${d.getDate()}日 ${WEEKDAY[d.getDay()]}`; },
        groupByDate(items, prefix, desc) {
          const map = new Map();
          items.forEach(item => {
            const key = prefix === 'past' ? (item.end_date || item.start_date || 'unknown') : (item.start_date || 'unknown');
            if (!map.has(key)) map.set(key, []);
            map.get(key).push(item);
          });
          const groups = Array.from(map.entries()).map(([date, list]) => ({ date, label: this.formatDateLabel(date), items: list, anchor: `${prefix}-${date}` }));
          groups.sort((a,b) => String(a.date).localeCompare(String(b.date)));
          if (desc) groups.reverse();
          return groups;
        },
        jumpTo(anchor) {
          if (!anchor) return;
          if (anchor.startsWith('past-')) this.collapsed.past = false;
          if (anchor.startsWith('upcoming-')) this.collapsed.upcoming = false;
          this.$nextTick(() => {
            const el = document.getElementById(anchor);
            if (el) {
              this.activeFlashAnchor = anchor;
              el.scrollIntoView({ behavior: 'smooth', block: 'start' });
              window.setTimeout(() => { if (this.activeFlashAnchor === anchor) this.activeFlashAnchor = ''; }, 1300);
            }
          });
        },
        scrollTop() { window.scrollTo({ top: 0, behavior: 'smooth' }); },
        async stepMonth(delta) {
          const idx = this.availableMonths.indexOf(this.currentMonth);
          if (idx < 0) return;
          const next = idx + delta;
          if (next >= 0 && next < this.availableMonths.length) {
            this.currentMonth = this.availableMonths[next];
            await this.loadMonth(this.currentMonth);
            this.prefetchNeighbors(this.currentMonth);
            window.scrollTo({ top: 0, behavior: 'smooth' });
          }
        }
      },
      mounted() { this.init(); }
    }).mount('#app');
  </script>
</body>
</html>
'''


def copy_brand_logos() -> None:
    src_map = {
        "765as.svg": "765as.svg",
        "cinderella.svg": "cinderella.svg",
        "million.svg": "million.svg",
        "sidem.svg": "sidem.svg",
        "sc.svg": "sc.svg",
        "gaku.svg": "gaku.svg",
        "valiv.svg": "valiv.svg",
    }
    src_dir = Path("svgtmp")
    dst_dir = Path("dist/assets/logos")
    dst_dir.mkdir(parents=True, exist_ok=True)
    for src_name, dst_name in src_map.items():
        src = src_dir / src_name
        if src.exists():
            shutil.copy2(src, dst_dir / dst_name)


def ensure_dirs() -> None:
    Path("data").mkdir(parents=True, exist_ok=True)
    Path("dist").mkdir(parents=True, exist_ok=True)
    Path("dist/data/months").mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_async_data(normalized: Dict[str, Any]) -> None:
    manifest = normalized.get("window", {}).copy()
    manifest["generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    write_json(Path("dist/data/manifest.json"), manifest)
    buckets = build_month_buckets(normalized)
    for month, payload in buckets.items():
        write_json(Path(f"dist/data/months/{month}.json"), payload)


def load_recent_cache(max_age_hours: int = 4) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    raw_path = Path("data/raw-window.json")
    normalized_path = Path("data/schedule-window.json")
    if not raw_path.exists() or not normalized_path.exists():
        return None
    age_seconds = dt.datetime.now().timestamp() - normalized_path.stat().st_mtime
    if age_seconds > max_age_hours * 3600:
        return None
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
    return raw, normalized


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch IM@S schedule and generate static page")
    parser.add_argument("--month", default=dt.date.today().strftime("%Y-%m"), help="center month, format YYYY-MM")
    args = parser.parse_args()

    ensure_dirs()
    center_month = args.month

    recent_cache = load_recent_cache(max_age_hours=4)
    if recent_cache is not None:
        raw, normalized = recent_cache
        print("[ok] using recent cached schedule data")
    else:
        try:
            raw = fetch_schedule_window(center_month, months_before=12, months_after=12)
            normalized = normalize(raw)
        except Exception as exc:
            cache = Path("data/schedule-window.json")
            if cache.exists():
                print(f"[warn] API failed, fallback to cache: {exc}")
                normalized = json.loads(cache.read_text(encoding="utf-8"))
                raw = {"error": str(exc), "cached": True}
            else:
                print(f"[error] API failed and no cache available: {exc}", file=sys.stderr)
                return 1
        write_json(Path("data/raw-window.json"), raw)
        write_json(Path("data/schedule-window.json"), normalized)
    write_async_data(normalized)
    copy_brand_logos()
    Path("dist/index.html").write_text(build_html(), encoding="utf-8")

    print("[ok] wrote data/schedule-window.json")
    print("[ok] wrote dist/data/manifest.json")
    print("[ok] wrote dist/index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
