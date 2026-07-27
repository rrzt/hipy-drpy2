
#!/usr/bin/env python3
"""
hhkan2.com - 好好看影视 观看脚本
功能：
  1. 浏览首页热门影视
  2. 按分类浏览（电影/剧集/动漫/综艺/短剧）
  3. 搜索影片
  4. 获取播放链接（通过API/详情页解析）

注意：本脚本依赖已安装的 requests + beautifulsoup4
pip install requests beautifulsoup4
"""

import requests
import re
import json
import sys
from urllib.parse import urljoin, urlparse, parse_qs

BASE_URL = "https://www.hhkan2.com/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.hhkan2.com/",
}

# 分类ID映射
CATEGORIES = {
    "1": "电影",
    "2": "连续剧",
    "3": "动漫",
    "4": "综艺纪录",
    "37": "短剧",
}

# ==========================
#  工具函数
# ==========================

def fetch_html(url, timeout=15):
    """请求页面，返回HTML文本"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text
    except requests.RequestException as e:
        print(f"  [✗] 请求失败: {e}")
        return None


def parse_items_from_html(html):
    """
    解析首页/分类页，提取所有影片条目
    返回: [ {title, score, status, link, img}, ... ]
    """
    items = []

    # 提取所有 voddetail 链接（影片详情页）
    vod_links = re.findall(
        r'<a[^>]*href=["\'](/voddetail/\d+\.html)["\'][^>]*>(.*?)</a>',
        html, re.DOTALL
    )
    # 提取图片
    img_map = {}
    img_pattern = re.compile(
        r'<img[^>]*src=["\']([^"\']+)["\'][^>]*data-original=["\']([^"\']+)["\']'
    )
    for m in img_pattern.finditer(html):
        img_map[m.group(1)] = m.group(2)

    seen_titles = set()
    for link, title_text in vod_links:
        # 清理标题
        title = re.sub(r'<[^>]+>', '', title_text).strip()
        if not title or len(title) > 40 or title in seen_titles:
            continue
        seen_titles.add(title)
        full_link = urljoin(BASE_URL, link)

        items.append({
            "title": title,
            "link": full_link,
            "score": "",
            "status": "",
            "img": "",
        })

    # 提取豆瓣评分和状态
    # 从纯文本中匹配
    text = re.sub(r'<[^>]+>', '\n', html)
    lines = [l.strip() for l in text.split('\n') if l.strip()]

    score_status_map = {}
    i = 0
    while i < len(lines) - 1:
        line = lines[i]
        # 豆瓣评分
        score_match = re.match(r'豆瓣[:：]([\d.]+)分', line)
        status_match = re.match(r'(正片|BT|HD[\w|]*|更新[\w\d]*|已完结|全[\d]+集)', line)

        if score_match:
            score = score_match.group(1)
            next_line = lines[i + 1] if i + 1 < len(lines) else ""
            # 下一行可能是状态
            next_status = ""
            if re.match(r'^(正片|BT|HD|更新|已完结|全\d)', next_line):
                next_status = next_line
                # 再下一行是标题
                name_line = lines[i + 2] if i + 2 < len(lines) else ""
            else:
                name_line = next_line

            if name_line and len(name_line) < 30:
                score_status_map[name_line] = f"⭐{score}分 {next_status}"

        elif status_match:
            status = status_match.group(1)
            name_line = lines[i + 1] if i + 1 < len(lines) else ""
            if name_line and len(name_line) < 30 and not re.match(r'^豆瓣|^正片|^BT|^HD', name_line):
                score_status_map[name_line] = f"[{status}]"

        i += 1

    # 合并评分和状态到items
    for item in items:
        for name, info in score_status_map.items():
            if name in item["title"] or item["title"] in name:
                if "⭐" in info:
                    item["score"] = info
                else:
                    item["status"] = info

    return items


# ==========================
#  核心功能
# ==========================

def browse_home():
    """浏览首页"""
    print("\n" + "=" * 50)
    print("  🏠 首页 - 热门推荐")
    print("=" * 50)
    html = fetch_html(BASE_URL)
    if not html:
        return []

    items = parse_items_from_html(html)
    # 去重 + 截取前30条
    seen = set()
    unique = []
    for item in items:
        if item["title"] not in seen:
            seen.add(item["title"])
            unique.append(item)

    for idx, item in enumerate(unique[:30], 1):
        score_str = f" {item['score']}" if item['score'] else ""
        status_str = f" {item['status']}" if item['status'] else ""
        print(f"  [{idx:2d}]{score_str}{status_str} {item['title']}")

    print(f"\n  ... 共 {len(unique)} 部影片")
    return unique


def browse_category(cat_id, page=1):
    """按分类浏览"""
    cat_name = CATEGORIES.get(cat_id, f"分类{cat_id}")
    url = urljoin(BASE_URL, f"/vodtype/{cat_id}-{page}.html")
    print(f"\n{'=' * 50}")
    print(f"  📂 {cat_name} - 第{page}页")
    print("=" * 50)

    html = fetch_html(url)
    if not html:
        return []

    items = parse_items_from_html(html)
    for idx, item in enumerate(items[:30], 1):
        score_str = f" {item['score']}" if item['score'] else ""
        print(f"  [{idx:2d}]{score_str} {item['title']}")

    print(f"\n  ... 共 {len(items)} 部")
    return items


def search_video(keyword, page=1):
    """搜索影片"""
    params = {
        "wd": keyword,
        "submit": "search",
    }
    url = f"{BASE_URL}vodsearch/{keyword}----------{page}---.html"
    print(f"\n{'=' * 50}")
    print(f"  🔍 搜索: \"{keyword}\"  第{page}页")
    print("=" * 50)

    html = fetch_html(url)
    if not html:
        # 尝试另一种搜索格式
        url = urljoin(BASE_URL, "vodsearch.html")
        try:
            resp = requests.post(
                url, data={"wd": keyword, "submit": "search"},
                headers=HEADERS, timeout=15
            )
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            html = resp.text
        except:
            print("  [✗] 搜索失败")
            return []

    items = parse_items_from_html(html)
    if not items:
        print("  [信息] 未找到相关结果")
        return []

    for idx, item in enumerate(items[:20], 1):
        score_str = f" {item['score']}" if item['score'] else ""
        print(f"  [{idx:2d}]{score_str} {item['title']}")

    return items


def get_detail(vod_url):
    """
    获取影片详情和播放链接
    返回: {title, desc, pic, play_list: [ {name, url}, ... ]}
    """
    print(f"\n[信息] 正在获取详情: {vod_url}")
    html = fetch_html(vod_url)
    if not html:
        return None

    # 提取标题
    title_match = re.search(r'<title>([^<]+)</title>', html)
    title = title_match.group(1).strip() if title_match else "未知"

    # 提取描述
    desc_match = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\']([^"\']+)["\']', html)
    desc = desc_match.group(1).strip() if desc_match else ""

    # 提取播放源和链接
    play_list = []
    # 常见的播放源结构: 多个播放组
    source_pattern = re.compile(
        r'<input[^>]*id=["\']playlist_(\d+)["\']value=["\']([^"\']+)["\']'
    )
    for m in source_pattern.finditer(html):
        source_id = m.group(1)
        encoded_data = m.group(2)
        # 解析播放链接 (通常是 名称$链接|名称$链接 格式)
        parts = encoded_data.split('#')
        for part in parts:
            if '$' in part:
                name, link = part.split('$', 1)
                if link.strip():
                    play_list.append({
                        "name": name.strip(),
                        "url": link.strip(),
                        "source_id": source_id,
                    })

    # 如果上面没找到，尝试从JS变量中提取
    if not play_list:
        # 查找类似 var play_lin = [...] 的结构
        js_match = re.search(r'player_llist\s*=\s*([^;]+)', html)
        if js_match:
            try:
                import ast
                data = ast.literal_eval(js_match.group(1).strip())
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, (list, tuple)) and len(item) >= 2:
                            play_list.append({"name": item[0], "url": item[1]})
            except:
                pass

    # 查找更直接的播放iframe
    iframe_match = re.search(
        r'<iframe[^>]*src=["\']([^"\']+play[^"\']+)["\']', html
    )
    if iframe_match and not play_list:
        play_list.append({"name": "iframe播放", "url": iframe_match.group(1)})

    result = {
        "title": title,
        "desc": desc[:200] + "..." if len(desc) > 200 else desc,
        "play_list": play_list,
    }

    # 显示详情
    print(f"\n  🎬 {title}")
    if desc:
        print(f"  📝 {result['desc']}")
    print(f"  📺 共 {len(play_list)} 个播放源")
    for idx, p in enumerate(play_list[:10], 1):
        print(f"    [{idx}] {p['name']}: {p['url'][:60]}{'...' if len(p['url']) > 60 else ''}")

    return result


def extract_direct_play_url(play_url):
    """
    从播放页尝试提取可直接播放的视频URL
    适用各种播放器(m3u8/flv/mp4)
    """
    print(f"\n[信息] 解析播放页: {play_url}")
    html = fetch_html(play_url)

    if not html:
        return None

    # 1. 查找视频URL (m3u8/mp4/flv)
    patterns = [
        r'url\s*[:=]\s*["\']([^"\']+\.(?:m3u8|mp4|flv))["\']',
        r'src\s*[:=]\s*["\']([^"\']+\.(?:m3u8|mp4|flv))["\']',
        r'video[^>]*src=["\']([^"\']+)["\']',
        r'var\s+url\s*=\s*["\']([^"\']+)["\']',
        r'<source[^>]*src=["\']([^"\']+)["\']',
        r'"url"\s*:\s*"([^"]+)"',
        r'"link"\s*:\s*"([^"]+)"',
        r'"play_url"\s*:\s*"([^"]+)"',
    ]

    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            video_url = match.group(1)
            # 解码可能的unicode
            video_url = video_url.encode('utf-8').decode('unicode_escape') if '\\u' in video_url else video_url
            print(f"  ✅ 发现视频链接: {video_url}")
            return video_url

    # 2. 查找iframe内嵌
    iframe = re.search(r'<iframe[^>]*src=["\']([^"\']+)["\']', html)
    if iframe:
        iframe_url = iframe.group(1)
        print(f"  🔗 内嵌播放器: {iframe_url}")
        return extract_direct_play_url(iframe_url)

    print("  [✗] 未找到可直接播放的视频链接")
    return None


# ==========================
#  交互菜单
# ==========================

def main_menu():
    """交互式主菜单"""
    current_items = []

    while True:
        print("\n" + "=" * 50)
        print("  🎬 好好看影视 - hhkan2.com")
        print("=" * 50)
        print("  1. 浏览首页热门")
        print("  2. 按分类浏览")
        print("  3. 搜索影片")
        print("  4. 查看已选择影片详情")
        print("  0. 退出")
        print("-" * 50)

        choice = input("  请选择 [0-4]: ").strip()

        if choice == "0":
            print("  再见！")
            break

        elif choice == "1":
            current_items = browse_home()

        elif choice == "2":
            print("\n  分类列表:")
            for cid, cname in CATEGORIES.items():
                print(f"    [{cid}] {cname}")
            cat_id = input("  选择分类: ").strip()
            page = input("  页码 (默认1): ").strip() or "1"
            if cat_id in CATEGORIES:
                current_items = browse_category(cat_id, int(page))

        elif choice == "3":
            keyword = input("  输入关键词: ").strip()
            if keyword:
                current_items = search_video(keyword)

        elif choice == "4":
            if not current_items:
                print("  [提示] 请先浏览或搜索")
                continue

            print("\n  当前列表 (前20条):")
            for idx, item in enumerate(current_items[:20], 1):
                print(f"    [{idx}] {item['title']}")
            try:
                sel = int(input("  选择编号查看详情: ").strip())
                if 1 <= sel <= len(current_items):
                    item = current_items[sel - 1]
                    detail = get_detail(item["link"])
                    if detail and detail["play_list"]:
                        print("\n  选择播放源:")
                        for idx, p in enumerate(detail["play_list"][:10], 1):
                            print(f"    [{idx}] {p['name']}")
                        try:
                            ps = int(input("  选择播放源编号: ").strip())
                            if 1 <= ps <= len(detail["play_list"][:10]):
                                play_src = detail["play_list"][ps - 1]
                                print(f"\n  🎯 播放地址: {play_src['url']}")
                                # 尝试解析直接链接
                                video_url = extract_direct_play_url(play_src["url"])
                                if video_url:
                                    print(f"\n  ✅ 可直接播放:")
                                    print(f"     {video_url}")
                                    if video_url.endswith('.m3u8'):
                                        print("  💡 提示: m3u8可用 VLC / IINA / PotPlayer 播放")
                                    else:
                                        print("  💡 提示: 可用浏览器直接打开播放")
                        except ValueError:
                            pass
            except (ValueError, IndexError):
                print("  [✗] 无效选择")

        else:
            print("  [✗] 无效选项")

    print("\n感谢使用！")


# ==========================
#  命令行快速模式
# ==========================

def quick_search(keyword):
    """命令行快速搜索并获取第一个结果的播放链接"""
    items = search_video(keyword)
    if items:
        first = items[0]
        print(f"\n  自动获取 \"{first['title']}\" 的详情...")
        detail = get_detail(first["link"])
        if detail and detail["play_list"]:
            first_play = detail["play_list"][0]
            print(f"\n  🎯 第一个播放源: {first_play['name']}")
            print(f"     {first_play['url']}")
            video_url = extract_direct_play_url(first_play["url"])
            if video_url:
                print(f"\n  📺 视频地址: {video_url}")


# ==========================
#  入口
# ==========================

if __name__ == "__main__":
    if len(sys.argv) > 1:
        # 命令行模式: python3 hhkan2_player.py 搜索关键词
        keyword = " ".join(sys.argv[1:])
        quick_search(keyword)
    else:
        # 交互模式
        main_menu()
