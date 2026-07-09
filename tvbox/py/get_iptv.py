import re
import os
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

SETTINGS_FILE = "py/config/settings.conf"     # 输出配置文件路径

EPG_URL = ""          # 写入 M3U 头部 x-tvg-url
LOGO_BASE = ""   # Logo 基础地址，最终为 BASE + 净化名 + .png
ENABLE_EPG = False                             # 是否写入 x-tvg-url
ENABLE_LOGO = False                            # 是否写入 tvg-logo
SANITIZE_DISPLAY = True                      # 是否用净化名替换可见频道名(默认保留模板名)
# 新增功能: 是否额外生成一份"模板外可自动归类频道"的补充播放列表
ENABLE_AUTOGROUP_EXTRA = True

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

# ============================================================================
# 新增: 输出增强配置的外置化 (EPG / Logo / 各开关改由配置文件管理)
# ----------------------------------------------------------------------------
# 沿用项目既有的纯文本配置风格: KEY = VALUE，# 注释，utf-8-sig 兼容 BOM，
# 不引入额外依赖(不使用 configparser，避免 section 头与 % 插值等约束)。
# ============================================================================
# 布尔型配置项(其余按字符串处理)
_BOOL_SETTINGS = {"ENABLE_EPG", "ENABLE_LOGO", "SANITIZE_DISPLAY", "ENABLE_AUTOGROUP_EXTRA"}

_DEFAULT_SETTINGS_TEMPLATE = """\
# ============================================================
# get_iptv 输出增强配置 (首次运行自动生成，可自由修改)
# 规则: KEY = VALUE; 以 # 开头为注释; 布尔值支持 true/false/1/0/yes/no
# 修改后无需改动脚本本体，下次运行自动生效
# ============================================================
"""

def _parse_bool(value, default=False):
    """宽松解析布尔值，无法识别时回退到 default。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    v = str(value).strip().lower()
    if v in ("1", "true", "yes", "on", "y"):
        return True
    if v in ("0", "false", "no", "off", "n", ""):
        return False
    return default

def ensure_settings_file(path=SETTINGS_FILE):
    """若配置文件不存在，则写入带注释的默认模板，方便用户直接编辑。"""
    if os.path.exists(path):
        return
    try:
        ensure_dir(path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(_DEFAULT_SETTINGS_TEMPLATE)
        logger.info(f"未检测到配置文件，已生成默认模板: {path}")
    except Exception as e:
        logger.error(f"生成默认配置文件失败: {e}")

def load_settings(path=SETTINGS_FILE):
    """
    从 KEY=VALUE 配置文件加载输出增强项，缺省沿用脚本内置默认值。
    - utf-8-sig 兼容记事本可能写入的 BOM
    - 空行 / # 开头行忽略
    - 仅按第一个 '=' 切分，值两端的成对引号自动去除
    - 未知键忽略，非法布尔值回退默认，读取异常时整体回退默认(不影响主流程)
    """
    # 以脚本当前全局值作为默认基准
    settings = {
        "EPG_URL": EPG_URL,
        "LOGO_BASE": LOGO_BASE,
        "ENABLE_EPG": ENABLE_EPG,
        "ENABLE_LOGO": ENABLE_LOGO,
        "SANITIZE_DISPLAY": SANITIZE_DISPLAY,
        "ENABLE_AUTOGROUP_EXTRA": ENABLE_AUTOGROUP_EXTRA,
    }
    if not os.path.exists(path):
        return settings
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, raw = line.partition("=")
                key = key.strip()
                if key not in settings:
                    continue  # 忽略未知键
                val = raw.strip()
                # 去除值两端成对引号
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if key in _BOOL_SETTINGS:
                    settings[key] = _parse_bool(val, default=settings[key])
                else:
                    settings[key] = val
        logger.info(f"已加载配置文件: {path}")
    except Exception as e:
        logger.error(f"读取配置文件失败，改用默认值: {e}")
    return settings

def apply_settings(settings):
    """把配置字典写回模块全局，供各输出函数(读取全局)生效。"""
    global EPG_URL, LOGO_BASE, ENABLE_EPG, ENABLE_LOGO, SANITIZE_DISPLAY, ENABLE_AUTOGROUP_EXTRA
    EPG_URL = settings["EPG_URL"]
    LOGO_BASE = settings["LOGO_BASE"]
    ENABLE_EPG = settings["ENABLE_EPG"]
    ENABLE_LOGO = settings["ENABLE_LOGO"]
    SANITIZE_DISPLAY = settings["SANITIZE_DISPLAY"]
    ENABLE_AUTOGROUP_EXTRA = settings["ENABLE_AUTOGROUP_EXTRA"]
    logger.info(
        "输出配置生效: EPG=%s Logo=%s 净化显示=%s 补充列表=%s"
        % (ENABLE_EPG, ENABLE_LOGO, SANITIZE_DISPLAY, ENABLE_AUTOGROUP_EXTRA)
    )

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

# ============================================================================
# 规则顺序即优先级，命中即返回。keywords 为子串包含匹配(大小写敏感，与网页版一致)，
# regex 为正则匹配。未命中任何规则返回 "其他"。
# ============================================================================
GROUP_RULES = [
    # ===== 优先级最高：央视与卫视 =====
    {'keywords': ['CCTV', 'cctv', '央视', 'cgtn', 'CGTN', 'CETV', 'cetv', '中国教育', '风云音乐', '风云足球', '风云剧场', '高尔夫', '怀旧剧场', '文化精品', '电视指南', '世界地理', '兵器科技', '女性时尚', '第一剧场', '卫生健康', '老故事', '中学生', '发现之旅'], 'group': '央视'},
    {'keywords': ['卫视'], 'group': '卫视'},  # 防止"湖南卫视"误分湖南

    # ===== 各省市 =====
    {'keywords': ['北京', 'BTV', 'BRTV', '淘', '萌宠TV'], 'group': '北京'},
    {'keywords': ['天津'], 'group': '天津'},
    {'keywords': ['上海'], 'group': '上海'},
    {'keywords': ['重庆'], 'group': '重庆'},
    {'keywords': ['河北'], 'group': '河北'},
    {'keywords': ['山西'], 'group': '山西'},
    {'keywords': ['内蒙古', '蒙语'], 'group': '内蒙古'},
    {'keywords': ['辽宁'], 'group': '辽宁'},
    {'keywords': ['吉林'], 'group': '吉林'},
    {'keywords': ['黑龙江'], 'group': '黑龙江'},
    {'keywords': ['江苏'], 'group': '江苏'},
    {'keywords': ['浙江'], 'group': '浙江'},
    {'keywords': ['安徽'], 'group': '安徽'},
    {'keywords': ['福建'], 'group': '福建'},
    {'keywords': ['江西'], 'group': '江西'},
    {'keywords': ['山东'], 'group': '山东'},
    {'keywords': ['河南'], 'group': '河南'},
    {'keywords': ['湖北'], 'group': '湖北'},
    {'keywords': ['湖南', '长沙', '宁乡', '浏阳', '株洲', '湘潭', '岳阳', '益阳', '常德', '娄底', '张家界', '怀化', '衡阳', '郴州', '湘西', '永州', '邵阳', '潇湘电影'], 'group': '湖南'},
    {'keywords': ['广东', '珠江', '大湾区'], 'group': '广东'},
    {'keywords': ['广西'], 'group': '广西'},
    {'keywords': ['海南'], 'group': '海南'},
    {'keywords': ['四川'], 'group': '四川'},
    {'keywords': ['贵州'], 'group': '贵州'},
    {'keywords': ['云南'], 'group': '云南'},
    {'keywords': ['西藏', '藏语'], 'group': '西藏'},
    {'keywords': ['陕西'], 'group': '陕西'},
    {'keywords': ['甘肃'], 'group': '甘肃'},
    {'keywords': ['青海'], 'group': '青海'},
    {'keywords': ['宁夏'], 'group': '宁夏'},
    {'keywords': ['新疆', '兵团'], 'group': '新疆'},

    # ===== 台湾细分 =====
    {'keywords': ['CatchPlay電影', 'CatchPlay电影', 'CinemaWorld', 'EYE TV戲劇', 'EYE TV戏剧', 'HITS', 'ROCK ACTION', 'ROCK Entertainment', 'ROCK Extreme', 'Warner TV', 'amc電影', 'amc电影', 'My Cinema Europe', '三立戲劇', '三立戏剧', '台灣戲劇', '台湾戏剧', '台灣電視劇', '台湾电视剧', '壹電視電影', '壹电视电影', '罪案偵緝', '罪案侦缉', '美亞電影', '美亚电影', '華藝影劇', '华艺影剧', '采昌影劇', '采昌影剧', '靖天戲劇', '靖天戏剧', '靖天映畫', '靖天映画', '靖天電影', '靖天电影', '靖洋戲劇', '靖洋戏剧', '龍華偶像', '龙华偶像', '龍華影劇', '龙华影剧', '龍華戲劇', '龙华戏剧', '龍華洋片', '龙华洋片', '龍華經典', '龙华经典', '龍華電影', '龙华电影', '影迷數位電影', '影迷数位电影', '愛爾達影劇', '爱尔达影剧'], 'group': '台湾-影视'},
    {'keywords': ['Arirang TV', 'CLASSICA HD', 'CMusic', 'LUXE TV Channel', 'Medici-arts', 'Mezzo Live HD', 'MTV Live', 'MTV綜合電視', 'MTV综合电视', 'TV5MONDE STYLE HD', 'TV5MONDE', 'tvN', '韓國娛樂', '韩国娱乐', '滾動力', '滚动力', '時尚', 'TVBS歡樂', 'TVBS欢乐', 'TVBS精采', 'TVBS精彩', '緯來精采', '纬来精彩', '中視菁采', '中视菁采'], 'group': '台湾-娱乐'},
    {'keywords': ['BBC World News', 'Bloomberg TV', 'CNBC Asia Channel', 'CNN International', 'Channel NewsAsia', 'Euronews', 'FRANCE24(English)', 'FRANCE24(French)', 'NHK新聞資訊', 'NHK新闻资讯', 'TaiwanPlus', 'TVBS新聞', 'TVBS新闻', '中視新聞', '中视新闻', '台視新聞', '台视新闻', '壹電視新聞', '壹电视新闻', '寰宇新聞', '寰宇新闻', '寰宇財經', '寰宇财经', '華視新聞資訊', '华视新闻资讯', '鏡電視新聞', '镜电视新闻', '靖天資訊', '靖天资讯', '非凡新聞', '非凡新闻', '半島英語新聞', '半岛英语新闻', '三立財經新聞', '三立财经新闻'], 'group': '台湾-新闻'},
    {'keywords': ['ELEVEN SPORTS', 'EUROSPORT', 'NBA動態', 'NBA动态', 'TRACE Sport Stars', '世界羽球雜誌', '世界羽球杂志', '博斯無限', '博斯无限', '博斯網球', '博斯网球', '博斯運動', '博斯运动', '博斯高球', '博斯魅力', '華視教育體育文化', '华视教育体育文化', '智林體育', '智林体育', '愛爾達亞運', '爱尔达亚运', '愛爾達體育', '爱尔达体育'], 'group': '台湾-体育'},
    {'keywords': ['Animax', 'CARTOONITO', 'CBeebies', 'DreamWorks', 'Nick Jr.', 'momo亲子', '尼克兒童', '尼克儿童', 'i-Fun動漫', 'i-Fun动漫', '曼迪日本', '靖天卡通', '靖洋卡通', '龍華動畫', '龙华动画', '靖天育樂', '靖天育乐'], 'group': '台湾-少儿'},
    {'keywords': ['HAPPY', '彩虹', '松視', '松视', '潘朵啦', '潘朵拉', '香蕉', '驚豔', '惊艳', 'JStar極限', 'JStar极限'], 'group': '台湾-成人'},
    {'keywords': ['亞洲旅遊', '亚洲旅游', '亞洲綜合', '亚洲综合', 'BBC Earth', 'BBC Lifestyle Channel', 'Discovery', 'DMAX', 'EVE', 'Global Trekker', 'HGTV', 'INULTRA', 'Lifetime', 'LOVE NATURE', 'PET CLUB TV', 'Smart知識', 'Smart知识', 'TECHStorm', 'TechStorm', 'Travel Channel', 'DaVinCi Learning達文西', 'DaVinCi Learning达文西', 'ELTV生活英語', 'ELTV生活英语', 'Asian Food Network', 'Food Network', 'EYE TV旅遊', 'EYE TV旅游', '歷史', '美食星球', '中視經典', '中视经典', '台視財經', '台视财经', '冠軍電視', '冠军电视', '國會', '国会', '影迷數位紀實', '影迷数位纪实', '視納華仁紀實', '视纳华仁纪实', '幸福空間居家', '幸福空间居家', '靖天日本', '德國之聲電視', '德国之声电视'], 'group': '台湾-生活'},
    {'keywords': ['GOOD TV', '人間衛視', '人间卫视', '佛衛電視慈悲', '佛卫电视慈悲', '唯心電視', '唯心电视', '大愛', '大爱', '好消息', '正德電視', '正德电视', '生命電視', '生命电视', '華藏衛視', '华藏卫视', '新唐人亞太', '新唐人亚太', '信吉藝文', '信吉艺文', '信吉電視', '信吉电视'], 'group': '台湾-宗教'},
    {'keywords': ['中視', '中视', '台視', '台视', '民視', '民视', '公視', '公视', '華視', '华视', 'TVBS', '公視2', '公视2', '公視3', '公视3', '公視台語', '公视台语', '台視綜合', '台视综合', '八大優', '三立綜合', '大立電視', '天天電視', '客家電視', '原住民族電視', '寰宇綜合', '寰宇综合', '壹電視資訊綜合', '壹电视资讯综合', '愛爾達綜合', '爱尔达综合', '華藝綜合', '华艺综合', '華藝灣', '华艺湾', '靖天綜合', '靖天综合', '靖天歡樂', '靖天欢乐', 'ETtoday綜合', 'ETtoday综合', '靖天國際', '靖天国际', '富立', '寰宇', '華藝', '华艺', 'momo'], 'group': '台湾-综合'},

    # ===== 港澳 / 日本 / 国际 =====
    {'keywords': ['无线', 'TVB', '翡翠', '明珠', '香港有线', '有线新闻', '凤凰', '鳳凰', 'NowTV', 'HKTV', 'ViuTV', '香港电台', '香港電台', 'RTHK', 'ATV', '亞洲電視', '奇妙電視', '澳视', '澳门'], 'group': '港澳'},
    {'keywords': ['NHK', 'NHK教育', 'NHK総合', '日本电视', '日本電視', '富士电视', '富士電視', 'TBS', '朝日电视', '朝日電視', '电视朝日', '電視朝日', 'WOWOW', '东京电视', '東京電視', 'BS Japan', 'BSジャパン', '日本BS', 'Animax Japan', 'NHK BS1', 'NHK BSプレミアム'], 'group': '日本'},
    {'keywords': ['CNN', 'BBC', 'Al Jazeera', 'Bloomberg', 'NHK World', 'France 24', 'Sky News', 'Euronews', 'TV5Monde', 'Russia Today', 'ABC', 'NBC', 'CBS', 'FOX', 'National Geographic', 'Discovery Channel', 'Animal Planet', 'History Channel', 'Cartoon Network', 'HBO', 'Warner', 'Disney', 'Paramount', 'Nick Jr.', 'HGTV', 'amc', 'Netflix'], 'group': '国际'},

    # ===== 品牌类 =====
    {'keywords': ['SiTV', 'SITV'], 'group': 'SiTV'},
    {'keywords': ['看天下精选', '百变课堂', '健康养生', '华语影院', '电竞天堂', '青春动漫', '宝宝动画', '星光院线', '星光影院', '谍战剧场', '全球大片', '热门剧场', '戏曲精选', '热门综艺', '百视通', 'bestv'], 'group': 'BesTV'},
    {'keywords': ['咪咕', '睛彩', '咪视通', '至臻视界', '咪视界'], 'group': '咪咕'},
    {'keywords': ['iHOT', 'IHOT', 'ihot'], 'group': 'iHOT'},
    {'keywords': ['欢笑剧场', '动漫秀场', '劲爆体育', '金色学堂', '游戏风云', '生活时尚', '乐游', '都市剧场', '东方财经', '新视觉', '法制天地', '纪实人文', '法治天地'], 'group': 'SiTV'},
    {'keywords': ['CHC', '影迷电影', '家庭影院', '求索', '重温经典', '中国天气', '中国交通', '先锋乒羽', '茶', '快乐垂钓', '优漫', '金鹰', '卡酷', '嘉佳', '炫动', '教育', '北京纪实', '科教', '梨园', '国学', '篮球', '汽摩', '环球奇观', '文物宝库', '武术世界', '天下', '收藏', '书画', '新动漫', '围棋', '弈坛春秋', '4K', '中华功夫', '环球旅游', '四海钓鱼', '优优宝贝', '生态环境'], 'group': '数字'},
    {'keywords': ['黑莓', '哒啵', '精品大剧', '古装剧场', '军旅剧场', '家庭剧场', '热播精选', '爱情喜剧', '动作电影', '惊悚悬疑', '金牌综艺', '精品体育', '精品萌宠', '农业致富', '精品综合', '中国功夫', '怡伴健康', '潮妈辣婆', '精品纪录', '超级电影', '超级体育', '超级综艺', '欢乐剧场', '超级电视剧', '军事评论', '炫舞未来', '武搏世界', '魅力潇湘', '东北热剧', '明星大片', 'NewTV', 'newtv', 'NEWTV'], 'group': 'NewTV'},
    {'regex': re.compile(r'^爱.{2}'), 'group': 'iHOT'},

    # ===== 兜底分类 =====
    {'keywords': ['教育', '学习', '课堂'], 'group': '教育'},
    {'keywords': ['影视', '影院', '电影', '剧场'], 'group': '影视'},
    {'keywords': ['综艺', '娱乐'], 'group': '综艺'},
    {'keywords': ['体育'], 'group': '体育'},
    {'keywords': ['纪录'], 'group': '纪录片'},
    {'keywords': ['广播', '之声', '调频', 'FM'], 'group': '广播'},
    {'keywords': ['测试', '回看', '备用', '广告'], 'group': '测试'},
    {'keywords': ['购物', '聚鲨环球精选', '快乐购', '嘉丽购', '購物'], 'group': '购物'},
]

def match_group(channel_name):
    """按 GROUP_RULES 顺序对频道名做自动分组，未命中返回 '其他'。"""
    if not channel_name:
        return '其他'
    for rule in GROUP_RULES:
        rx = rule.get('regex')
        if rx is not None and rx.search(channel_name):
            return rule['group']
        for kw in rule.get('keywords', []):
            if kw in channel_name:
                return rule['group']
    return '其他'

# 省级分组顺序 (用于自动分组输出排序，替代网页版依赖浏览器的 localeCompare)
PROVINCE_GROUPS = [
    '北京', '天津', '上海', '重庆', '河北', '山西', '内蒙古', '辽宁', '吉林', '黑龙江',
    '江苏', '浙江', '安徽', '福建', '江西', '山东', '河南', '湖北', '湖南', '广东',
    '广西', '海南', '四川', '贵州', '云南', '西藏', '陕西', '甘肃', '青海', '宁夏', '新疆',
]
_TIER_PRIORITY = {'数字': 3, 'SiTV': 4, 'NewTV': 5, 'BesTV': 6, 'iHOT': 7, '咪咕': 8}
_PLACE_PRIORITY = {'港澳': 11, '日本': 12, '国际': 13}

def group_sort_key(g):
    """
    分组排序键 (移植并强化自 inx.html 的 groupPriority):
    央视 -> 卫视 -> 各省(按地理顺序) -> 数字/SiTV/NewTV/BesTV/iHOT/咪咕 ->
    台湾各细分 -> 港澳/日本/国际 -> 其余(按名称)。
    返回三元组用于稳定排序。
    """
    if g == '央视':
        return (0, 0, g)
    if g == '卫视':
        return (1, 0, g)
    if g in PROVINCE_GROUPS:
        return (2, PROVINCE_GROUPS.index(g), g)
    if g in _TIER_PRIORITY:
        return (_TIER_PRIORITY[g], 0, g)
    if g.startswith('台湾'):
        return (10, 0, g)
    if g in _PLACE_PRIORITY:
        return (_PLACE_PRIORITY[g], 0, g)
    return (999, 0, g)

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

def fetch_all_channels(tv_urls, max_workers=5):
    """
    增强(性能): 将并发抓取聚合逻辑抽出为独立函数。
    原先每个 process_iptv_task 都会各自抓取一遍相同的源，重复请求；
    抽出后可在主流程中"抓取一次、多任务复用"(模板任务 + 自动分组任务共享)。
    返回聚合后的 all_channels (OrderedDict: 分类 -> [(name, url), ...])。
    """
    all_channels = OrderedDict()
    success_count = 0
    fail_count = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_url = {executor.submit(fetch_channels, url): url for url in tv_urls}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                data = future.result()
                if data:
                    success_count += 1
                    for cat, chans in data.items():
                        if cat not in all_channels:
                            all_channels[cat] = []
                        all_channels[cat].extend(chans)
                else:
                    fail_count += 1
            except Exception as e:
                fail_count += 1
                logger.error(f"源 {url} 异常: {e}")

    logger.info(f"数据获取完毕: 成功解析 {success_count} 个源，失败/空数据 {fail_count} 个源。")
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
    - tvg-id / tvg-name 使用净化名，利于 EPG 与 Logo 匹配
    - tvg-logo 由 LOGO_BASE + 净化名 + .png 生成 (可通过 ENABLE_LOGO 关闭)
    - 可见名 display_name 保持模板/源提供的名称
    """
    if clean_name is None:
        clean_name = sanitize_channel_name(display_name) or display_name

    attrs = f'tvg-id="{clean_name}" tvg-name="{clean_name}"'
    if ENABLE_LOGO and LOGO_BASE:
        attrs += f' tvg-logo="{LOGO_BASE}{clean_name}.png"'
    attrs += f' group-title="{category}"'
    return f'#EXTINF:-1 {attrs},{display_name}'

def _m3u_header():
    """增强: 带 EPG 的 M3U 头 (x-tvg-url)。"""
    if ENABLE_EPG and EPG_URL:
        return f'#EXTM3U x-tvg-url="{EPG_URL}"\n'
    return "#EXTM3U\n"

def generate_outputs(channels, template_channels, m3u_path, txt_path):
    """生成文件 - 路径参数化 (增强: 注入 EPG / Logo / 净化后的 tvg 标签)"""
    written_urls = set()

    # 安全地确保输出目录存在
    ensure_dir(m3u_path)
    ensure_dir(txt_path)

    try:
        with write_lock:
            with open(m3u_path, "w", encoding="utf-8") as m3u, \
                 open(txt_path, "w", encoding="utf-8") as txt:

                # 增强: 写入带 x-tvg-url 的 M3U 头
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

                        # 增强: 净化名只计算一次，供 tvg-id / tvg-name / Logo 复用
                        clean_name = sanitize_channel_name(channel_key_name) or channel_key_name
                        # 可见名: 默认保留模板名; 开启 SANITIZE_DISPLAY 则用净化名
                        display_name = clean_name if SANITIZE_DISPLAY else channel_key_name

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

def generate_auto_grouped_playlist(source_channels, m3u_path, txt_path):
    """
    对给定频道集合执行"净化 -> 自动分组 -> 去重 -> 优先级排序"，
    产出一份不依赖模板的、自动组织好的播放列表。
    典型用途: 把"模板未覆盖但可自动归类"的源频道整理成补充列表，避免遗漏。

    source_channels: 可为 OrderedDict(分类 -> [(name,url)]) 或直接的 [(name,url)] 列表。
    """
    # 归一化输入为 (name, url) 列表
    flat = []
    if isinstance(source_channels, dict):
        for _, chans in source_channels.items():
            flat.extend(chans)
    else:
        flat = list(source_channels)

    if not flat:
        logger.info(f"自动分组: 无可整理的频道，跳过 {m3u_path}")
        return

    # 分组聚合 + 全局 URL 去重(按 $ 前真实地址)
    grouped = OrderedDict()
    seen_base_urls = set()
    for name, url in flat:
        url = (url or "").strip()
        if not name or not url:
            continue
        base = url.split("$")[0].strip()
        if not base or base in seen_base_urls:
            continue
        seen_base_urls.add(base)

        group = match_group(name)
        grouped.setdefault(group, []).append((name, url))

    if not grouped:
        logger.info(f"自动分组: 去重后无有效频道，跳过 {m3u_path}")
        return

    # 按分组优先级排序
    ordered_groups = sorted(grouped.keys(), key=group_sort_key)

    ensure_dir(m3u_path)
    ensure_dir(txt_path)
    try:
        with write_lock:
            with open(m3u_path, "w", encoding="utf-8") as m3u, \
                 open(txt_path, "w", encoding="utf-8") as txt:

                m3u.write(_m3u_header())
                for group in ordered_groups:
                    txt.write(f"\n{group},#genre#\n")
                    for name, url in grouped[group]:
                        clean_name = sanitize_channel_name(name) or name
                        display_name = clean_name if SANITIZE_DISPLAY else name
                        m3u.write(_build_extinf(display_name, group, clean_name) + "\n")
                        m3u.write(f"{url}\n")
                        txt.write(f"{display_name},{url}\n")

        total = sum(len(v) for v in grouped.values())
        logger.info(f"自动分组输出完成: {m3u_path} ({len(ordered_groups)} 组 / {total} 频道)")
    except Exception as e:
        logger.error(f"自动分组输出失败: {e}")

def generate_unmatched_report(unmatched_template, unmatched_source, report_file):
    """生成未匹配报告 (增强: 源中多余频道按智能分组归类展示)"""
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

            # 增强: 源中多余频道原先仅按"源自带分类"堆列，可读性差；
            # 改为用 match_group 智能归类，并按分组优先级排序，便于评估
            # 哪些频道值得补进模板。
            f.write("\n\n## 源中有但模板无 (按智能分组)\n")
            auto_grouped = OrderedDict()
            for cat, chans in unmatched_source.items():
                for name, _url in chans:
                    g = match_group(name)
                    auto_grouped.setdefault(g, []).append(name)

            for g in sorted(auto_grouped.keys(), key=group_sort_key):
                unique_names = list(OrderedDict.fromkeys(auto_grouped[g]))
                if unique_names:
                    f.write(f"\n{g},#genre#\n")
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
                      auto_clean=True, all_channels=None, extra_m3u=None, extra_txt=None):
    """
    处理单个IPTV任务的封装函数
    增强: 新增 all_channels 参数支持"预抓取复用"，避免多任务重复请求同一批源；
          新增 extra_m3u/extra_txt，可为该任务额外产出"模板外可自动归类"的补充列表；
          新增返回值 (matched, unmatched_tmpl, unmatched_src)，供调用方在任务间
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

    # 新增功能: 把模板没收录、但能被智能规则归类的源频道，整理成补充列表
    if extra_m3u and extra_txt:
        generate_auto_grouped_playlist(unmatched_src, extra_m3u, extra_txt)

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

    # 0. 加载输出增强配置(不存在则生成带注释的默认模板；异常则回退内置默认)
    ensure_settings_file(SETTINGS_FILE)
    apply_settings(load_settings(SETTINGS_FILE))

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
        # 新增: 模板外可自动归类的补充列表
        extra_m3u="lib/iptv_extra.m3u" if ENABLE_AUTOGROUP_EXTRA else None,
        extra_txt="lib/iptv_extra.txt" if ENABLE_AUTOGROUP_EXTRA else None,
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
