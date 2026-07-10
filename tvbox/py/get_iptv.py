import re
import os
import json
import unicodedata
import requests
import logging
import shutil
import threading
from collections import OrderedDict
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# === 配置日志 ===
def setup_logger():
    # 确保日志目录存在
    os.makedirs("logs", exist_ok=True)
    
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    logger.handlers.clear() # 清除已有 handler 避免重复
    
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    # 控制台输出
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 文件输出
    file_handler = logging.FileHandler("logs/iptv_update.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    return logger

logger = setup_logger()

# 全局锁，用于文件写入
write_lock = threading.Lock()

def ensure_dir(file_path):
    """确保文件所在的目录存在"""
    dirname = os.path.dirname(file_path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)

_DASH_VARIANTS_RE = re.compile(r'[－—﹣–─]')
_MULTI_SPACE_RE = re.compile(r'\s+')

def normalize_channel_name(name):
    """
    频道名称归一化，用于匹配前的预处理（不用于最终显示名）：
    - 全角字符转半角（如 ＣＣＴＶ１ -> CCTV1）
    - 统一各类破折号/连字符为标准 "-"
    - 合并连续空白并去除首尾空格
    """
    if not name:
        return ""
    name = unicodedata.normalize('NFKC', name)
    name = _DASH_VARIANTS_RE.sub('-', name)
    name = _MULTI_SPACE_RE.sub(' ', name).strip()
    return name

# ============================================================================
# 与 normalize_channel_name 职责不同:
#   normalize_channel_name -> 供"匹配"用，尽量保留信息、只做等价归一
#   sanitize_channel_name  -> 供"显示 / tvg-id / Logo 文件名"用，做激进清洗
# 强化点(相对网页版): 前置 NFKC，使 ＣＣＴＶ１ 这类全角输入也能正确识别为 CCTV1。
# ============================================================================
# 强化(相对网页版 sanitizeName):
#  1) CCTV 与序号间容忍空白/连字符(CCTV-13 / CCTV 13 也能归一为 CCTV13)
#  2) 括号成对/孤立集合扩全(补齐 NFKC 后的半角 () {}，以及 CJK 角括号「」『』)
_CCTV_RE = re.compile(r'^CCTV[\s\-]*(\d+)\s*([+K]?)', re.IGNORECASE)
_HD_RE = re.compile(r'超清|超高清|极清|高清|标清|HD|SD', re.IGNORECASE)
_BRACKET_PAIR_RE = re.compile(r'[\[\(（【｛\{<＜「『].*?[\]\)）】｝\}>＞」』]')
_BRACKET_SOLO_RE = re.compile(r'[\[\]\(\)（）【】｛｝\{\}<>＜＞「」『』]')
_DASH_UNDERSCORE_RE = re.compile(r'[-_]')

def sanitize_channel_name(name):
    """频道名净化: CCTV 归一化 / 去清晰度后缀 / 去括号 / 去连字符下划线。"""
    if not name:
        return ""
    # 强化: 先做全角->半角等价折叠 + 破折号变体归一，
    # 兼容 ＣＣＴＶ１ / ＨＤ / CCTV－13 等写法
    name = unicodedata.normalize('NFKC', name)
    name = _DASH_VARIANTS_RE.sub('-', name)

    # CCTV 系列特殊处理
    m = _CCTV_RE.match(name)
    if m:
        num = m.group(1)
        suffix = (m.group(2) or '')
        if num == '5' and suffix == '+':
            return 'CCTV5+'
        if num in ('4', '8') and suffix.upper() == 'K':
            return 'CCTV' + num + 'K'
        return 'CCTV' + num

    clean = name
    # 强化(顺序修正): 先去掉成对括号及其内容，再剥离清晰度后缀。
    # 网页版先按 HD 截断再删括号，会把 "台名(HD)" 从括号中间截断而残留孤立 "("。
    clean = _BRACKET_PAIR_RE.sub('', clean)

    # 去掉"超清/超高清/极清/高清/标清/HD/SD"及其后所有内容
    hd = _HD_RE.search(clean)
    if hd:
        clean = clean[:hd.start()]

    # 再次清除残留的孤立括号符号(含半角 () {} 与角括号)
    clean = _BRACKET_SOLO_RE.sub('', clean)
    # 去掉短横线 / 下划线，并去除首尾空白
    clean = _DASH_UNDERSCORE_RE.sub('', clean).strip()
    return clean

def get_session():
    """创建一个带有重试机制的requests Session"""
    session = requests.Session()
    # 增强: 增加 total 上限，并对常见的服务端临时错误状态码也进行重试
    retry = Retry(
        total=3,
        connect=3,
        read=2,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    # 增强: 部分源站会拒绝无 User-Agent 的请求，补充常见浏览器 UA 提升成功率
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    })
    return session

def load_urls_from_file(file_path):
    """从文本文件加载URL列表"""
    urls = []
    if not os.path.exists(file_path):
        logger.warning(f"URL配置文件未找到: {file_path}")
        return urls

    try:
        # 使用 utf-8-sig 安全过滤由于记事本编辑可能产生的 \ufeff BOM 头
        with open(file_path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
        logger.info(f"从 {file_path} 加载了 {len(urls)} 个源")
    except Exception as e:
        logger.error(f"读取URL文件失败: {e}")
    return urls

def parse_template(template_file):
    """解析模板文件"""
    template_channels = OrderedDict()
    current_category = None

    try:
        # 使用 utf-8-sig 避免首行解析出错
        with open(template_file, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                if "#genre#" in line:
                    current_category = line.split(",")[0].strip()
                    # 修复: 若同一分类在模板中重复出现，不应清空之前已收集的频道
                    if current_category not in template_channels:
                        template_channels[current_category] = []
                elif current_category:
                    channel_name = line.split(",")[0].strip()
                    # 修复(崩溃预防): 跳过空白频道名，避免后续 match_channels 中
                    # variants 列表为空导致 IndexError
                    if channel_name:
                        template_channels[current_category].append(channel_name)
    except FileNotFoundError:
        logger.warning(f"模板文件未找到: {template_file}")
        return None 

    return template_channels

def fetch_channels(url):
    """从URL获取频道列表"""
    channels = OrderedDict()

    # 使用上下文管理器确保 socket 资源正确释放
    with get_session() as session:
        try:
            # 增强: 拆分连接/读取超时，连接更快失败，读取给足时间
            with session.get(url, timeout=(10, 30)) as response:
                response.raise_for_status()
                raw_bytes = response.content

        except Exception as e:
            logger.error(f"处理 {url} 时出错: {e}")
            return channels

    # 修复(编码): 国内 IPTV 源大量使用 GBK/GB2312 编码，直接强制 utf-8 会导致乱码。
    # 跳过缓慢的 chardet(apparent_encoding) 探测，改用 utf-8 优先、GBK 兜底的快速解码策略。
    try:
        text_content = raw_bytes.decode('utf-8')
    except UnicodeDecodeError:
        try:
            text_content = raw_bytes.decode('gbk')
        except UnicodeDecodeError:
            text_content = raw_bytes.decode('utf-8', errors='ignore')

    lines = [line.strip() for line in text_content.splitlines() if line.strip()]
    if not lines:
        return channels

    # 修复(健壮性): 部分 M3U 源在 #EXTINF 之前有较多头部/注释行，仅扫描前10行可能误判为 txt 格式；
    # 优先判断标准 #EXTM3U 头，并放宽扫描窗口
    is_m3u = lines[0].strip().upper().startswith("#EXTM3U") or any(
        "#EXTINF" in line for line in lines[:30]
    )

    if is_m3u:
        DEFAULT_CATEGORY = "默认分类"
        DEFAULT_NAME = "未知频道"
        current_category = DEFAULT_CATEGORY
        current_name = DEFAULT_NAME

        re_group = re.compile(r'group-title="([^"]*)"')
        # 强化正则: 优先取最后一个带引号属性之后的逗号分隔内容，兼容频道名本身含逗号的情况；
        # 若没有任何带引号属性，则回退为 EXTINF: 后第一个逗号之后的内容
        re_name_after_quote = re.compile(r'"\s*,(.*)$')
        re_name_fallback = re.compile(r'#EXTINF:[^,]*,(.*)$')

        for line in lines:
            if line.startswith("#EXTINF"):
                # 修复(分类状态重置): 每条 EXTINF 若未显式声明 group-title，
                # 不应继续沿用上一条频道残留的分类，而应重置为默认分类
                group_match = re_group.search(line)
                current_category = group_match.group(1).strip() if group_match else DEFAULT_CATEGORY

                name_match = re_name_after_quote.search(line) or re_name_fallback.search(line)
                # 修复(状态重置): 未匹配到名称时重置为默认值，避免沿用上一条频道的名称
                current_name = name_match.group(1).strip() if name_match else DEFAULT_NAME
            elif not line.startswith("#") and "://" in line:
                if current_category not in channels:
                    channels[current_category] = []
                if current_name and current_name != DEFAULT_NAME:
                    channels[current_category].append((current_name, line))
                current_name = DEFAULT_NAME
    else:
        current_category = None
        for line in lines:
            if "#genre#" in line:
                current_category = line.split(",")[0].strip()
                if current_category not in channels:
                    channels[current_category] = []
            # 修复: 分类名为空字符串时属于合法但异常的边界情况，用 is not None 判断
            # 避免因空字符串的假值特性而错误丢弃整段内容
            elif current_category is not None and "," in line:
                parts = line.split(",", 1)
                if len(parts) == 2:
                    name, url_part = parts
                    if name.strip() and url_part.strip():
                        channels[current_category].append((name.strip(), url_part.strip()))

    return channels

SOURCE_STATE_FILE = "py/config/source_state.json"
# 连续失败达到该阈值才判定源"真正失效"；阈值内的失败视为瞬时网络抖动。
# 跨境线路对同一源的抓取本身存在偶发超时/连接失败，如果把"这一次没抓到"
# 直接当成"这条源已失效"处理，会导致每次生成结果里的线路数量/顺序反复
# 来回变化，而源其实并未真正下线或替换。
SOURCE_FAIL_THRESHOLD = 3

def load_source_state(path=SOURCE_STATE_FILE):
    """加载各源 URL 的连续失败计数 + 最近一次成功抓取结果 (跨进程持久化)。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"读取源状态文件失败，视为无历史状态重新计数: {e}")
        return {}

def save_source_state(state, path=SOURCE_STATE_FILE):
    try:
        ensure_dir(path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"写入源状态文件失败: {e}")

def fetch_all_channels(tv_urls, max_workers=5):
    """
    增强(性能): 将并发抓取聚合逻辑抽出为独立函数。
    原先每个 process_iptv_task 都会各自抓取一遍相同的源，重复请求；
    抽出后可在主流程中"抓取一次、多任务复用"。
    返回聚合后的 all_channels (OrderedDict: 分类 -> [(name, url), ...])。

    修复(线路来回跳变): 单次抓取失败不再直接当作该源真正失效——
    连续失败次数 < SOURCE_FAIL_THRESHOLD 时，回退复用 source_state.json 中
    该源上一次成功抓取的结果，使本次输出的线路数量/顺序保持稳定；
    只有连续失败达到阈值，才判定为真正失效，不再回退，交由后续
    match_channels 把它当作确实缺失来处理(可被清理/进入未匹配报告)。
    """
    all_channels = OrderedDict()
    success_count = 0
    fallback_count = 0   # 抓取失败但未达阈值，回退使用上次成功结果
    dead_count = 0        # 抓取失败且判定为真正失效(达阈值，或从未成功过)

    state = load_source_state()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_url = {executor.submit(fetch_channels, url): url for url in tv_urls}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            entry = state.get(url, {"fail_streak": 0, "channels": {}})

            try:
                data = future.result()
            except Exception as e:
                data = None
                logger.error(f"源 {url} 异常: {e}")

            if data:
                # 抓取成功: 清零连续失败计数，缓存本次结果供下次抖动时回退用
                success_count += 1
                entry["fail_streak"] = 0
                entry["channels"] = {cat: [[n, u] for n, u in chans] for cat, chans in data.items()}
                use_data = data
            else:
                entry["fail_streak"] = entry.get("fail_streak", 0) + 1
                if entry["fail_streak"] < SOURCE_FAIL_THRESHOLD and entry.get("channels"):
                    # 未达阈值且有历史成功结果可用: 视为瞬时抖动，回退复用
                    fallback_count += 1
                    use_data = {cat: [(n, u) for n, u in chans] for cat, chans in entry["channels"].items()}
                    logger.warning(
                        f"源 {url} 本次抓取失败(连续 {entry['fail_streak']}/{SOURCE_FAIL_THRESHOLD} 次)，"
                        f"判定为瞬时抖动，回退复用上次成功结果"
                    )
                else:
                    # 达到/超过阈值，或从未有过成功结果: 判定为真正失效
                    dead_count += 1
                    use_data = None
                    if entry["fail_streak"] >= SOURCE_FAIL_THRESHOLD:
                        logger.error(
                            f"源 {url} 已连续失败 {entry['fail_streak']} 次(阈值 {SOURCE_FAIL_THRESHOLD})，判定为真正失效"
                        )

            state[url] = entry

            if use_data:
                for cat, chans in use_data.items():
                    if cat not in all_channels:
                        all_channels[cat] = []
                    all_channels[cat].extend(chans)

    save_source_state(state)

    logger.info(
        f"数据获取完毕(共 {len(tv_urls)} 个源): 成功 {success_count} 个，"
        f"判定瞬时抖动回退 {fallback_count} 个，判定真正失效 {dead_count} 个。"
    )
    return all_channels

def match_channels(template_channels, all_channels):
    matched = OrderedDict()
    unmatched_template = OrderedDict()

    # 1. 数据扁平化
    flattened_source_channels = []
    for cat, chans in all_channels.items():
        for name, url in chans:
            flattened_source_channels.append({
                # 增强(频道名归一化): 匹配前统一全角/半角、破折号变体、多余空白，
                # 提升如 ＣＣＴＶ１ / CCTV－1 / CCTV 1 等变体的识别率
                'norm_name': normalize_channel_name(name).lower(),
                'name': name,
                'url': url,
                'cat': cat,
                'key': f"{name}_{url}"
            })

    used_channel_keys = set()

    # 初始化
    for cat in template_channels:
        matched[cat] = OrderedDict()
        unmatched_template[cat] = []

    # 2. 匹配逻辑
    for category, tmpl_names in template_channels.items():
        for tmpl_name in tmpl_names:
            
            # 去重并解析变体
            variants_raw = [n.strip() for n in tmpl_name.split("|") if n.strip()]
            variants = list(OrderedDict.fromkeys(variants_raw))

            # 修复(崩溃预防): 理论上 parse_template 已过滤空名称，这里再做一层防御，
            # 避免 variants 为空时 variants[0] 抛出 IndexError
            if not variants:
                continue

            primary_name = variants[0]
            found_for_this_template = False

            for variant in variants:
                variant_lower = normalize_channel_name(variant).lower()
                if not variant_lower:
                    continue

                # 强化正则: 两端都加边界限制
                # 结尾: 匹配到字符串末尾($) 或 非字母数字且非加号([^a-z0-9\+])，防止 CCTV5 匹配 CCTV5+
                # 开头: 匹配字符串开头(^) 或 非字母数字([^a-z0-9])，防止变体作为子串被更长的名称
                #       误匹配（例如变体 "5" 不应命中 "CCTV15" 中间的 "5"）
                pattern = re.compile(
                    r'(?:^|[^a-z0-9])' + re.escape(variant_lower) + r'(?:$|[^a-z0-9\+])'
                )

                for src in flattened_source_channels:
                    if src['key'] in used_channel_keys:
                        continue

                    # 使用正则搜索
                    if pattern.search(src['norm_name']):
                        if primary_name not in matched[category]:
                            matched[category][primary_name] = []

                        matched[category][primary_name].append((src['name'], src['url']))

                        used_channel_keys.add(src['key'])
                        found_for_this_template = True

            if not found_for_this_template:
                unmatched_template[category].append(tmpl_name)

    # 3. 找出源中未使用的频道
    unmatched_source = OrderedDict()
    for src in flattened_source_channels:
        if src['key'] not in used_channel_keys:
            if src['cat'] not in unmatched_source:
                unmatched_source[src['cat']] = []
            unmatched_source[src['cat']].append((src['name'], src['url']))

    return matched, unmatched_template, unmatched_source

def is_ipv6(url):
    return "://[" in url

def _build_extinf(display_name, category, clean_name=None):
    """
    - tvg-id / tvg-name 使用净化名，利于播放器识别同一频道
    - 可见名 display_name 保持模板/源提供的名称
    """
    if clean_name is None:
        clean_name = sanitize_channel_name(display_name) or display_name

    attrs = f'tvg-id="{clean_name}" tvg-name="{clean_name}"'
    attrs += f' group-title="{category}"'
    return f'#EXTINF:-1 {attrs},{display_name}'

def _m3u_header():
    return "#EXTM3U\n"

def generate_outputs(channels, template_channels, m3u_path, txt_path):
    """生成文件 - 路径参数化"""
    written_urls = set()

    # 安全地确保输出目录存在
    ensure_dir(m3u_path)
    ensure_dir(txt_path)

    try:
        with write_lock:
            with open(m3u_path, "w", encoding="utf-8") as m3u, \
                 open(txt_path, "w", encoding="utf-8") as txt:

                m3u.write(_m3u_header())

                for category in template_channels:
                    if category not in channels or not channels[category]:
                        continue

                    txt.write(f"\n{category},#genre#\n")

                    for channel_key_name, channel_list in channels[category].items():

                        unique_urls = []
                        seen_base_urls = set()

                        for _, url in channel_list:
                            url = url.strip()
                            if not url:
                                continue
                            # 修复(URL去重): 去重应基于 "$" 分隔符前的真实播放地址，
                            # 而非原始字符串。不同源可能给同一条播放地址附加不同的
                            # 追踪后缀(如 $token=xxx)，原先按原始字符串去重会导致
                            # 同一条真实流地址被重复写入多次。
                            base_for_dedup = url.split("$")[0].strip()
                            if not base_for_dedup:
                                continue
                            if base_for_dedup not in seen_base_urls and base_for_dedup not in written_urls:
                                unique_urls.append(url)
                                seen_base_urls.add(base_for_dedup)
                                written_urls.add(base_for_dedup)

                        # 净化名供 tvg-id / tvg-name 使用；可见名保留模板名
                        clean_name = sanitize_channel_name(channel_key_name) or channel_key_name
                        display_name = channel_key_name

                        total_lines = len(unique_urls)
                        for idx, url in enumerate(unique_urls, 1):
                            base_url = url.split("$")[0].strip()
                            suffix_name = "IPV6" if is_ipv6(url) else "IPV4"

                            meta_suffix = f"$LR•{suffix_name}"
                            if total_lines > 1:
                                meta_suffix += f"•{total_lines}『线路{idx}』"

                            final_url = f"{base_url}{meta_suffix}"

                            m3u.write(_build_extinf(display_name, category, clean_name) + "\n")
                            m3u.write(f"{final_url}\n")

                            txt.write(f"{display_name},{final_url}\n")

        logger.info(f"输出完成: {m3u_path}, {txt_path}")
    except Exception as e:
        logger.error(f"写入输出文件失败: {e}")

def generate_unmatched_report(unmatched_template, unmatched_source, report_file):
    """生成未匹配报告"""
    total_template_lost = sum(len(v) for v in unmatched_template.values())
    
    # 如果未指定报告文件路径，则仅计算丢失数量，不执行文件写入
    if not report_file:
        return total_template_lost

    ensure_dir(report_file)

    try:
        with open(report_file, "w", encoding="utf-8") as f:
            f.write(f"# 未匹配报告 {datetime.now()}\n")
            f.write(f"# 模板未匹配数: {total_template_lost}\n\n")
            f.write("## 模板中有但源中无\n")
            for cat, names in unmatched_template.items():
                if names:
                    f.write(f"\n{cat},#genre#\n")
                    for name in list(OrderedDict.fromkeys(names)):
                        f.write(f"{name},\n")

            # 源中多余频道按其"源自带分类"归类展示
            f.write("\n\n## 源中有但模板无\n")
            for cat, chans in unmatched_source.items():
                unique_names = list(OrderedDict.fromkeys(name for name, _url in chans))
                if unique_names:
                    f.write(f"\n{cat},#genre#\n")
                    for name in unique_names:
                        f.write(f"{name},\n")
        logger.info(f"报告已生成: {report_file}")
        return total_template_lost
    except Exception as e:
        logger.error(f"生成报告失败: {e}")
        return 0

def remove_unmatched_from_template(template_file, unmatched_template):
    backup_file = template_file + ".backup"
    try:
        shutil.copy2(template_file, backup_file)
        with open(template_file, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()

        new_lines = []
        current_cat = None
        to_remove = {cat: set(names) for cat, names in unmatched_template.items()}

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue
            if "#genre#" in stripped:
                current_cat = stripped.split(",")[0].strip()
                new_lines.append(line)
                continue
            if current_cat:
                name = stripped.split(",")[0].strip()
                if current_cat in to_remove and name in to_remove[current_cat]:
                    continue
                new_lines.append(line)
            else:
                # 修复: 若不在任何 category 内的内容（如异常格式），不应被错误丢弃
                new_lines.append(line)

        with open(template_file, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        logger.info(f"已从模板 {template_file} 移除无效频道")
    except Exception as e:
        logger.error(f"更新模板失败: {e}")

def process_iptv_task(template_file, tv_urls, output_m3u, output_txt, report_file,
                      auto_clean=True, all_channels=None):
    """
    处理单个IPTV任务的封装函数
    增强: 新增 all_channels 参数支持"预抓取复用"，避免多任务重复请求同一批源；
          返回值 (matched, unmatched_tmpl, unmatched_src)，供调用方在任务间
          传递"未匹配"数据(例如把任务1的未匹配频道追加进任务2的模板)。
          解析失败(模板不存在)时返回 (None, None, None)，调用方需做空值判断。
    """
    logger.info(f"=== 开始处理任务: {template_file} ===")
    
    template = parse_template(template_file)
    if not template:
        return None, None, None

    # 增强: 若未传入预抓取数据，则自行抓取(保持向后兼容)
    if all_channels is None:
        logger.info(f"开始从 {len(tv_urls)} 个源获取数据...")
        all_channels = fetch_all_channels(tv_urls)

    logger.info("开始匹配频道...")
    matched, unmatched_tmpl, unmatched_src = match_channels(template, all_channels)

    generate_outputs(matched, template, output_m3u, output_txt)
    lost_count = generate_unmatched_report(unmatched_tmpl, unmatched_src, report_file)

    if auto_clean and lost_count > 0:
        logger.info(f"清理 {lost_count} 个无效频道...")
        remove_unmatched_from_template(template_file, unmatched_tmpl)
    
    logger.info(f"=== 任务完成: {template_file} ===\n")
    return matched, unmatched_tmpl, unmatched_src

def append_unmatched_to_template(unmatched_template, target_template_file):
    """
    新增功能: 把"任务1未匹配到的模板频道"追加进另一个模板文件(如 iptv_test.txt)，
    以便后续任务继续尝试匹配 / 持续观察这些频道后续是否恢复可用。

    设计要点:
    - 幂等: 已存在于目标模板对应分类下的频道不会被重复追加，可放心多次运行脚本
    - 目标文件不存在时自动创建
    - 追加时保留原始频道名写法(含 "变体1|变体2" 语法)，不做任何改写，
      确保后续 match_channels 的匹配行为与来源模板完全一致
    - 复用 parse_template "同一分类可重复出现、不清空之前频道" 的既有特性，
      直接在文件末尾新增一段 "分类,#genre#" 块，无需就地插入编辑

    返回本次实际追加的频道数量(0 表示无需追加)。
    """
    total_lost = sum(len(v) for v in (unmatched_template or {}).values())
    if total_lost == 0:
        logger.info(f"任务未匹配数为 0，无需追加到 {target_template_file}")
        return 0

    # 读取目标模板中已有的频道，避免重复追加(幂等)
    existing = parse_template(target_template_file) or OrderedDict()
    existing_sets = {cat: set(names) for cat, names in existing.items()}

    append_lines = []
    appended_count = 0
    for cat, names in unmatched_template.items():
        # 去重且保持原始出现顺序
        unique_names = list(OrderedDict.fromkeys(names))
        new_names = [n for n in unique_names if n not in existing_sets.get(cat, set())]
        if not new_names:
            continue
        append_lines.append(f"{cat},#genre#")
        append_lines.extend(f"{n}," for n in new_names)
        appended_count += len(new_names)

    if not appended_count:
        logger.info(f"未匹配频道均已存在于 {target_template_file}，无需重复追加")
        return 0

    try:
        ensure_dir(target_template_file)
        file_has_content = os.path.exists(target_template_file) and os.path.getsize(target_template_file) > 0
        with open(target_template_file, "a", encoding="utf-8") as f:
            if file_has_content:
                f.write("\n")
            f.write(f"# 以下 {appended_count} 个频道为其他任务未匹配到的频道，"
                    f"自动追加于 {datetime.now()}\n")
            f.write("\n".join(append_lines) + "\n")
        logger.info(f"已将 {appended_count} 个未匹配频道追加进 {target_template_file}")
        return appended_count
    except Exception as e:
        logger.error(f"追加未匹配频道到 {target_template_file} 失败: {e}")
        return 0

if __name__ == "__main__":
    # === 配置区 ===
    URLS_FILE = "py/config/urls.txt"

    # 1. 加载源
    TV_URLS = load_urls_from_file(URLS_FILE)
    if not TV_URLS:
        logger.warning("未从文件中加载到URL，使用空列表")
        TV_URLS = [] 

    # 增强(性能): 全部源只抓取一次，供后续所有任务复用
    logger.info(f"开始从 {len(TV_URLS)} 个源统一获取数据...")
    ALL_CHANNELS = fetch_all_channels(TV_URLS) if TV_URLS else OrderedDict()

    # === 任务1: 主列表 ===
    _matched1, unmatched_tmpl1, _unmatched_src1 = process_iptv_task(
        template_file="py/config/iptv.txt",
        tv_urls=TV_URLS,
        output_m3u="lib/iptv.m3u",
        output_txt="lib/iptv.txt",
        report_file="py/config/iptv.log",
        auto_clean=False,
        all_channels=ALL_CHANNELS,
    )

    # === 新增: 把任务1未匹配到的频道追加进测试列表模板，再执行任务2 ===
    TEST_TEMPLATE_FILE = "py/config/iptv_test.txt"
    if unmatched_tmpl1:
        append_unmatched_to_template(unmatched_tmpl1, TEST_TEMPLATE_FILE)

    # === 任务2: 测试列表 (追加后必然存在，除非任务1无未匹配且此前也无测试模板文件) ===
    if os.path.exists(TEST_TEMPLATE_FILE):
        process_iptv_task(
            template_file=TEST_TEMPLATE_FILE,
            tv_urls=TV_URLS,
            output_m3u="lib/iptv_test.m3u",
            output_txt="lib/iptv_test.txt",
            report_file=None,
            auto_clean=False,
            all_channels=ALL_CHANNELS,
        )
    else:
        logger.info(f"未检测到测试配置 {TEST_TEMPLATE_FILE}，跳过测试生成。")
