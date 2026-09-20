"""I0 来源契约探测脚本：验证三市场真实接口契约并录制原始响应。

仅用于开发期验证，不属于生产包。产物写入 tools/probe/out/；
整理脱敏后的样本再复制到 tests/fixtures/。

用法：
    python tools/probe/probe_sources.py --market us
    python tools/probe/probe_sources.py --market cn
    python tools/probe/probe_sources.py --market hk
    python tools/probe/probe_sources.py --market all

SEC 要求真实 User-Agent：设置环境变量 SEC_UA_EMAIL 后再探测 US。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent / "out"

UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
SEC_UA_EMAIL = os.environ.get("SEC_UA_EMAIL", "configure-email@example.com")
UA_SEC = f"reports-fetcher probe {SEC_UA_EMAIL}"


def log(*args) -> None:
    print(*args, flush=True)


def save(name: str, data, note: str = "") -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"  [saved] {path.name}  {note}")


def head_rows(recent: dict, forms: set[str], n: int = 12) -> list[dict]:
    """从并行数组中取前 n 条目标 form 的行。"""
    rows = []
    for i, form in enumerate(recent["form"]):
        if form in forms:
            rows.append({k: recent[k][i] for k in (
                "form", "filingDate", "reportDate", "accessionNumber", "primaryDocument")})
            if len(rows) >= n:
                break
    return rows


# ---------------------------------------------------------------- US / EDGAR

def probe_us() -> None:
    log("== US: SEC EDGAR ==")
    s = requests.Session()
    s.headers["User-Agent"] = UA_SEC
    log(f"UA: {UA_SEC}")

    # 1) ticker -> CIK 全表
    r = s.get("https://www.sec.gov/files/company_tickers.json", timeout=30)
    log(f"[1] company_tickers.json -> {r.status_code}, {len(r.content)} bytes")
    r.raise_for_status()
    entries = list(r.json().values())
    interested = {"AAPL", "BABA", "GOOG", "GOOGL", "BRK-A", "BRK-B", "BF-B"}
    hits = [e for e in entries if e["ticker"] in interested]
    log(f"    total={len(entries)}; 类股/样例命中: {[ (e['ticker'], e['cik_str']) for e in hits ]}")
    save("us_company_tickers_sample", {"total": len(entries), "hits": hits})
    time.sleep(0.3)

    cik = next(e["cik_str"] for e in entries if e["ticker"] == "AAPL")

    # 2) submissions recent
    r = s.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", timeout=30)
    log(f"[2] submissions AAPL -> {r.status_code}, {len(r.content)} bytes")
    r.raise_for_status()
    sub = r.json()
    recent = sub["filings"]["recent"]
    keys = list(recent.keys())
    lengths = {k: len(v) for k, v in recent.items() if isinstance(v, list)}
    forms_present = {}
    for f_ in recent["form"]:
        forms_present[f_] = forms_present.get(f_, 0) + 1
    periodic = head_rows(recent, {"10-Q", "10-K", "10-Q/A", "10-K/A", "20-F"})
    log(f"    name={sub.get('name')} recent数组长度={lengths['form']}")
    log(f"    form分布(前10): {sorted(forms_present.items(), key=lambda x: -x[1])[:10]}")
    log(f"    filings.files={sub['filings'].get('files')}")
    save("us_submissions_aapl_recent_sample", {
        "name": sub.get("name"), "cik": sub.get("cik"), "tickers": sub.get("tickers"),
        "recent_keys": keys, "recent_row_count": lengths["form"],
        "form_distribution_top": dict(sorted(forms_present.items(), key=lambda x: -x[1])[:15]),
        "periodic_rows_head": periodic,
        "filings_files": sub["filings"].get("files"),
    })
    time.sleep(0.3)

    # 3) 历史文件形态（若存在）
    files = sub["filings"].get("files") or []
    if files:
        name = files[-1]["name"]  # 最老的一段
        r = s.get(f"https://data.sec.gov/submissions/{name}", timeout=30)
        log(f"[3] 历史文件 {name} -> {r.status_code}, {len(r.content)} bytes")
        r.raise_for_status()
        hist = r.json()
        hist_keys = list(hist.keys())
        hist_lengths = {k: len(v) for k, v in hist.items() if isinstance(v, list)}
        tail_rows = []
        if "form" in hist:
            for i in range(len(hist["form"]) - 1, max(-1, len(hist["form"]) - 6), -1):
                tail_rows.append({k: hist[k][i] for k in (
                    "form", "filingDate", "reportDate", "accessionNumber", "primaryDocument") if k in hist})
        log(f"    历史文件顶层键={hist_keys[:8]} 行数={hist_lengths.get('form')}")
        save("us_submissions_aapl_histfile_sample", {
            "url": name, "top_level_keys": hist_keys,
            "array_lengths": hist_lengths, "tail_rows": tail_rows,
        })


# ---------------------------------------------------------------- CN / 巨潮

def _cn_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA_BROWSER,
        "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/notice",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*",
    })
    return s


def _cn_topsearch(s: requests.Session, keyword: str):
    r = s.post("http://www.cninfo.com.cn/new/information/topSearch/query",
               data={"keyWord": keyword, "maxNum": 10}, timeout=30)
    log(f"    topSearch[{keyword}] -> {r.status_code}, {len(r.content)} bytes")
    r.raise_for_status()
    return r.json()


def _cn_list(s: requests.Session, stock: str, column: str, se_date: str,
             page: int = 1, page_size: int = 30, category: str | None = None):
    data = {
        "pageNum": page, "pageSize": page_size, "column": column,
        "tabName": "fulltext", "plate": "", "stock": stock, "searchkey": "",
        "secid": "", "category": category or "", "trade": "", "seDate": se_date,
        "sortName": "", "sortType": "", "isHLtitle": "false",
    }
    r = s.post("http://www.cninfo.com.cn/new/hisAnnouncement/query",
               data=data, timeout=30)
    log(f"    hisAnnouncement[{stock} col={column} p={page}] -> {r.status_code}, {len(r.content)} bytes")
    r.raise_for_status()
    return r.json()


CN_CATEGORY = ("category_ndbg_szsh;category_bndbg_szsh;"
               "category_yjdbg_szsh;category_sjdbg_szsh")


def probe_cn() -> None:
    log("== CN: 巨潮资讯 ==")
    s = _cn_session()

    # 0) 预热首页收集 Cookie
    try:
        r0 = s.get("http://www.cninfo.com.cn/new/index", timeout=30)
        log(f"[0] 预热首页 -> {r0.status_code}; cookies={list(s.cookies.keys())}")
    except Exception as e:  # noqa: BLE001
        log(f"[0] 预热首页失败（继续尝试）: {e}")

    # 1) topSearch：沪市样本
    arr = _cn_topsearch(s, "600519")
    log(f"    600519 -> {[(a.get('code'), a.get('orgId'), a.get('zwjc'), a.get('category')) for a in arr][:3]}")
    org_600519 = next(a["orgId"] for a in arr if a.get("code") == "600519")
    time.sleep(0.5)
    # 深市样本
    arr2 = _cn_topsearch(s, "000001")
    org_000001 = next(a["orgId"] for a in arr2 if a.get("code") == "000001")
    log(f"    000001 -> orgId={org_000001}")
    save("cn_topsearch_samples", {
        "sh_600519": arr[:5], "sz_000001": arr2[:5],
        "orgId_600519": org_600519, "orgId_000001": org_000001,
    })
    time.sleep(0.5)

    # 2) 定期报告列表：column 对照实验（szse vs sse，沪市股票）
    se_date = "2023-09-01~2026-09-20"
    for col in ("szse", "sse"):
        try:
            j = _cn_list(s, f"600519,{org_600519}", col, se_date, category=CN_CATEGORY)
            anns = j.get("announcements") or []
            log(f"    column={col}: total={j.get('totalAnnouncement')} "
                f"hasMore={j.get('hasMore')} 本页={len(anns)}")
            if col == "szse":
                save("cn_hisann_600519_szse_p1", {
                    "total": j.get("totalAnnouncement"), "hasMore": j.get("hasMore"),
                    "page_count": len(anns),
                    "fields_of_first": sorted(anns[0].keys()) if anns else [],
                    "rows_head": [
                        {k: a.get(k) for k in (
                            "secCode", "secName", "announcementTitle",
                            "announcementTime", "adjunctUrl", "announcementTypeName" if "announcementTypeName" in a else "columnId")}
                        for a in anns[:8]],
                })
        except Exception as e:  # noqa: BLE001
            log(f"    column={col} 失败: {e}")
        time.sleep(0.5)

    # 3) 深市股票 + 翻页验证（用返回正确的 column）
    try:
        j = _cn_list(s, f"000001,{org_000001}", "szse", se_date, category=CN_CATEGORY)
        anns = j.get("announcements") or []
        log(f"    000001 col=szse: total={j.get('totalAnnouncement')} 本页={len(anns)}")
        titles = [a["announcementTitle"] for a in anns[:10]]
        log(f"    标题样例: {titles[:5]}")
        save("cn_hisann_000001_szse_p1", {
            "total": j.get("totalAnnouncement"), "page_count": len(anns),
            "titles_head": titles,
            "first_row_full": anns[0] if anns else {},
        })
        # 翻页：第二页应返回不同的 adjunctUrl
        if j.get("hasMore"):
            j2 = _cn_list(s, f"000001,{org_000001}", "szse", se_date,
                          page=2, category=CN_CATEGORY)
            anns2 = j2.get("announcements") or []
            overlap = {a["adjunctUrl"] for a in anns} & {a["adjunctUrl"] for a in anns2}
            log(f"    第2页={len(anns2)}条, 与第1页重叠={len(overlap)} (应为0)")
            save("cn_hisann_000001_szse_p2", {
                "page_count": len(anns2), "overlap_with_p1": len(overlap),
                "titles_head": [a["announcementTitle"] for a in anns2[:8]],
            })
    except Exception as e:  # noqa: BLE001
        log(f"    深市/翻页失败: {e}")

    # 4) PDF 可达性与 magic bytes（只下载第一份的头部）
    try:
        first = _cn_list(s, f"600519,{org_600519}", "szse", se_date, category=CN_CATEGORY)
        adj = (first.get("announcements") or [])[0]["adjunctUrl"]
        pdf_url = "http://static.cninfo.com.cn/" + adj
        rp = s.get(pdf_url, timeout=60, stream=True)
        chunk = next(rp.iter_content(1024))
        rp.close()
        log(f"    PDF HEAD {pdf_url} -> {rp.status_code}; magic={chunk[:5]!r}")
        save("cn_pdf_check", {"url": pdf_url, "status": rp.status_code,
                              "magic_bytes_hex": chunk[:8].hex()})
    except Exception as e:  # noqa: BLE001
        log(f"    PDF 检查失败: {e}")


# ---------------------------------------------------------------- HK / 披露易

def _hk_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA_BROWSER,
        "Referer": "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh",
        "Accept": "*/*",
    })
    return s


def _hk_stockid(s: requests.Session, code: str):
    r = s.get("https://www1.hkexnews.hk/search/prefix.do", params={
        "callback": "callback", "lang": "ZH", "type": "A",
        "name": code, "market": "SEHK"}, timeout=30)
    log(f"    prefix.do[{code}] -> {r.status_code}, {len(r.content)} bytes")
    r.raise_for_status()
    text = r.text
    m = re.match(r"^\s*callback\((.*)\)\s*;?\s*$", text, re.S)
    payload = json.loads(m.group(1)) if m else json.loads(text)
    infos = payload.get("stockInfo") or []
    for it in infos:
        if it.get("code") == code:
            return it
    return infos[0] if infos else None


def _get_with_retry(s: requests.Session, url: str, params: dict, tries: int = 3):
    last = None
    for i in range(tries):
        try:
            return s.get(url, params=params, timeout=30)
        except requests.RequestException as e:  # noqa: BLE001
            last = e
            log(f"    [retry {i + 1}/{tries}] {type(e).__name__}: {str(e)[:110]}")
            time.sleep(1.5 * (i + 1))
    raise last


def _hk_search(s: requests.Session, stock_id, from_yyyymmdd: str, to_yyyymmdd: str,
               t1code: str = "40000", title: str = "", lang: str = "ZH"):
    """已验证契约（2026-09-20）：GET 深链 titlesearch.xhtml，服务端渲染 HTML。

    有效日期参数是 from/to（YYYYMMDD）；fromDate/toDate 会被忽略。
    """
    params = {
        "lang": lang, "category": "0", "market": "SEHK", "searchType": "1",
        "documentType": "-1", "t1code": t1code, "t2Gcode": "-2", "t2code": "-2",
        "stockId": str(stock_id), "title": title,
        "from": from_yyyymmdd, "to": to_yyyymmdd,
    }
    r = _get_with_retry(s, "https://www1.hkexnews.hk/search/titlesearch.xhtml", params)
    log(f"    titlesearch.xhtml[t1={t1code} title={title or '-'}] -> "
        f"{r.status_code}, {len(r.content)} bytes")
    r.raise_for_status()
    return {"params": params, "record_count": _hk_count(r.text),
            "rows": _hk_parse_rows(r.text)}, r.text


def _hk_count(html: str) -> str | None:
    m = re.search(r"共有 (\d+) 紀錄", html)
    return m.group(1) if m else None


def _hk_parse_rows(html: str) -> list[dict]:
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        if ".pdf" not in tr.lower():
            continue
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        headline = re.search(r'<div class="headline">(.*?)</div>', tr, re.S)
        headline_text = (re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", headline.group(1))).strip()
                         if headline else "")
        subcat = re.search(r"\[(.+?)\]", headline_text)
        link = re.search(r'href="([^"]+\.pdf)"', tr)
        ltxt = re.search(r'href="[^"]+\.pdf"[^>]*>\s*(.*?)\s*</a>', tr, re.S)
        link_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", ltxt.group(1))).strip() if ltxt else ""
        size = re.search(r'attachment_filesize">([^<]+)<', tr)
        rows.append({
            "release_datetime": cells[0] if cells else "",      # 發放時間: DD/MM/YYYY HH:MM
            "stock_code": cells[1] if len(cells) > 1 else "",   # 可能含人民币柜台第二代码
            "stock_name": cells[2] if len(cells) > 2 else "",
            "headline": headline_text,                            # 头条类别 - [子类别]
            "subcategory": subcat.group(1) if subcat else "",
            "title": link_text,
            "file_link": link.group(1) if link else None,
            "file_size": size.group(1) if size else "",
        })
    return rows


def probe_hk() -> None:
    log("== HK: 披露易（契约已迁移：titlesearch.xhtml GET 深链，旧 titleSearcherJson.do 已 404） ==")
    s = _hk_session()

    # 1) stockId 映射（腾讯 00700 日历年结；新鸿基 0016 六月年结）
    ids = {}
    for code in ("00700", "0016"):
        info = _hk_stockid(s, code)
        log(f"    {code} -> {info}")
        ids[code] = info
        time.sleep(0.4)
    save("hk_prefix_samples", ids)

    # 2) 消融测试：全新会话直接深链检索（不预热页面），验证是否依赖 Cookie/预热
    fresh = _hk_session()
    try:
        ablation, _ = _hk_search(fresh, ids["00700"]["stockId"], "20230920", "20260920")
        log(f"    免Cookie消融: count={ablation['record_count']} rows={len(ablation['rows'])} "
            f"(与预热后一致即不依赖)")
        save("hk_ablation_nocookie", {"ok": True, "record_count": ablation["record_count"],
                                      "rows": len(ablation["rows"])})
    except Exception as e:  # noqa: BLE001
        log(f"    免Cookie消融失败: {type(e).__name__}（结论：保守要求先预热页面）")
        save("hk_ablation_nocookie", {"ok": False, "error": type(e).__name__})
    time.sleep(0.4)

    # 3) 00700 财务类（t1=40000）3 年
    res, html = _hk_search(s, ids["00700"]["stockId"], "20230920", "20260920")
    for row in res["rows"]:
        log(f"      [{row['subcategory']}] {row['title'][:40]}  {row['release_datetime'][:16]}")
    save("hk_search_00700_40000_3y", res)
    (OUT / "hk_search_00700_40000_3y.html").write_text(html, encoding="utf-8")
    time.sleep(0.4)

    # 4) 0016（六月年结）财务类 3 年：跨年标题形态
    res16, _ = _hk_search(s, ids["0016"]["stockId"], "20230920", "20260920")
    for row in res16["rows"][:6]:
        log(f"      [{row['subcategory']}] {row['title'][:40]}")
    save("hk_search_0016_40000_3y", res16)
    time.sleep(0.4)

    # 5) QTR-HK 验证：t1=10000 + 标题关键词 業績
    resq, _ = _hk_search(s, ids["00700"]["stockId"], "20260101", "20260920",
                         t1code="10000", title="業績")
    for row in resq["rows"]:
        log(f"      [{row['subcategory']}] {row['title'][:44]}")
    save("hk_search_00700_10000_yeji_2026", resq)
    time.sleep(0.4)

    # 6) 10 年窗口：单页返回能力
    res10, _ = _hk_search(s, ids["00700"]["stockId"], "20160920", "20260920")
    log(f"    10年窗口: count={res10['record_count']} 本页rows={len(res10['rows'])}")
    save("hk_search_00700_40000_10y", {"record_count": res10["record_count"],
                                       "rows_returned": len(res10["rows"]),
                                       "params": res10["params"]})

    # 7) PDF 可达性与 magic bytes
    try:
        link = res["rows"][0]["file_link"]
        rp = s.get("https://www1.hkexnews.hk" + link, timeout=60, stream=True)
        chunk = next(rp.iter_content(1024))
        rp.close()
        log(f"    PDF {link[:56]}... -> {rp.status_code}; magic={chunk[:5]!r}")
        save("hk_pdf_check", {"url": "https://www1.hkexnews.hk" + link,
                              "status": rp.status_code, "magic_bytes_hex": chunk[:8].hex()})
    except Exception as e:  # noqa: BLE001
        log(f"    PDF 检查失败: {e}")


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["us", "cn", "hk", "all"], default="all")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.market in ("us", "all"):
        probe_us()
    if args.market in ("cn", "all"):
        probe_cn()
    if args.market in ("hk", "all"):
        probe_hk()
    log(f"done. raw outputs -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
