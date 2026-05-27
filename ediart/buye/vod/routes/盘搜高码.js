const axios = require("axios");
const http = require("http");
const https = require("https");
const CryptoJS = require("crypto-js");
const dayjs = require("dayjs");

const _http = axios.create({
  timeout: 60 * 1000,
  httpsAgent: new https.Agent({ keepAlive: true, rejectUnauthorized: false }),
  httpAgent: new http.Agent({ keepAlive: true }),
  baseURL: "http://192.168.50.50:1514", // 替换成自己的盘搜地址
});

const init = async (server) => {
  if (store.init) return;
  store.redis = server.redis;
  store.log = server.log;
  store.drives = server.drives;
  store.init = true;
};

const detail = async ({ id }) => {
  const ids = !Array.isArray(id) ? [id] : id;
  const _id = ids[0];

  const result = {
    list: [],
  };

  for (const drive of store.drives) {
    if (drive.matchShare(_id)) {
      const vod = await drive.getVod(_id);
      if (vod) {
        vod.vod_id = _id;
        result.list.push(vod);
      }
      break;
    }
  }
  return result;
};

const search = async ({ page, quick, wd }) => {
  const result = {
    list: [],
    page: 1,
    pagecount: 1,
    total: 1,
  };

  const panTypes = {
    quark: "quark",
    uc: "uc",
    pikpak: "pikpak",
    xunlei: "xunlei",
    a123: "123",
    a189: "tianyi",
    a139: "mobile",
    a115: "115",
    baidu: "baidu"
  };

  const panPic = {
    ali: "https://xget.xi-xu.me/gh/power721/alist-tvbox/raw/refs/heads/master/web-ui/public/ali.jpg",
    quark: "https://xget.xi-xu.me/gh/power721/alist-tvbox/raw/refs/heads/master/web-ui/public/quark.png",
    uc: "https://xget.xi-xu.me/gh/power721/alist-tvbox/raw/refs/heads/master/web-ui/public/uc.png",
    pikpak: "https://xget.xi-xu.me/gh/power721/alist-tvbox/raw/refs/heads/master/web-ui/public/pikpak.jpg",
    xunlei: "https://xget.xi-xu.me/gh/power721/alist-tvbox/raw/refs/heads/master/web-ui/public/thunder.png",
    '123': "https://xget.xi-xu.me/gh/power721/alist-tvbox/raw/refs/heads/master/web-ui/public/123.png",
    tianyi: "https://xget.xi-xu.me/gh/power721/alist-tvbox/raw/refs/heads/master/web-ui/public/189.png",
    mobile: "https://xget.xi-xu.me/gh/power721/alist-tvbox/raw/refs/heads/master/web-ui/public/139.jpg",
    '115': "https://xget.xi-xu.me/gh/power721/alist-tvbox/raw/refs/heads/master/web-ui/public/115.jpg",
    'baidu': "https://xget.xi-xu.me/gh/power721/alist-tvbox/raw/refs/heads/master/web-ui/public/baidu.jpg",
  };

  const orderMap = store.drives
    .map((o) => o.key)
    .reduce((map, key, idx) => {
      map[key] = idx;
      return map;
    }, {});

  // ==================== 高优先级画质关键词（用于后续排序） ====================
  const qualityKeywords = [
    // 优先级从高到低
    'HDR', '杜比', 'DV',
    'REMUX', 'HQ', "臻彩",'高码', '高画质',
    '60FPS', '60帧', '高帧率', '60HZ',
    "4K", "2160P"
  ];

  // 构造 filter 参数传给盘搜后端（后端会帮你做 include/exclude 过滤）
  const filterParam = {
    include: qualityKeywords,           // 只保留包含这些关键词的资源
    exclude: ["枪版", "预告", "彩蛋"],   // 如有需要排除的词在这里加
  };

  const res = await _http.post("/api/search", {
    kw: wd,
    cloud_types: store.drives.map((d) => panTypes[d.key]).filter(Boolean),
    filter: filterParam,                 // ← 关键：把过滤交给后端
    // ext: { referer: "https://dm.xueximeng.com" },
  });

  const ret = res.data;
  if (ret.code !== 0) {
    throw new Error(ret.message || "请求失败");
  }

  // ------------------ 链接校验部分保持不变 ------------------
  const rawItems = [];
  for (const key in ret.data.merged_by_type || {}) {
    const panKey = Object.keys(panTypes).find((k) => panTypes[k] === key);
    const drive = store.drives.find((o) => o.key === panKey);
    const pic = panPic[key];

    for (const row of ret.data.merged_by_type[key] || []) {
      rawItems.push({ row, panKey, drive, pic });
    }
  }

  if (rawItems.length === 0) return result;

  const uniqueLinks = [...new Set(rawItems.map(it => it.row.url))];
  const VALID_STATUS = ["valid_links"];
  let validLinksSet = new Set();

  try {
    const checkRes = await _http.post(
      "http://192.168.50.50:7024/api/v1/links/check",
      {
        links: uniqueLinks,
        selected_platforms: [
          "quark", "uc", "tianyi", "pan123", "pan115", "xunlei", "cmcc", "baidu"
        ],
      },
      { timeout: 30000 }
    );

    for (const status of VALID_STATUS) {
      (checkRes.data[status] || []).forEach(link => validLinksSet.add(link));
    }
  } catch (e) {
    store.log?.error("[盘搜校验链接失败] " + e.message);
    uniqueLinks.forEach(l => validLinksSet.add(l));
  }

  const filteredItems = rawItems.filter(it => validLinksSet.has(it.row.url));
  // --------------------------------------------------------------

  // ------------------ 组装 vod（和原来一样） ------------------
  const currentTime = dayjs();

  for (const item of filteredItems) {
    const row = item.row;
    const drive = item.drive;
    const pic = item.pic;

    const rowTime = dayjs(row.datetime);
    const timeDiff = currentTime.diff(rowTime, 'minute');
    const dt = dayjs(timeDiff <= 70 && timeDiff >= 0 ? "0001-01-01T00:00:00Z" : row.datetime);
    const source = row.source ? row.source.replace(/plugin:/gi, '插件:').replace(/tg:/gi, '频道:') : "";

    result.list.push({
      vod_id: row.url,
      vod_name: row.note,
      vod_pic: pic,
      vod_remarks: `${source || ""} | ${item.panKey || ""} | ${dt.format("MMDDYY")}`,
      time: dt.unix(),
      pan: item.panKey,
    });
  }

  const completedKeywords = ["完结", "全集", "已完成", "全"];

  const getQualityScore = (name) => {
    const upper = name.toUpperCase();
    let score = 0,
      cnt = 0;
    for (let i = 0; i < qualityKeywords.length; i++) {
      if (upper.includes(qualityKeywords[i].toUpperCase())) {
        score += qualityKeywords.length - i;
        cnt++;
      }
    }
    return score + cnt;
  };

  const getCount = (name, arr) => {
    const upper = name.toUpperCase();
    let c = 0;
    for (const kw of arr) {
      if (upper.includes(kw.toUpperCase())) c++;
    }
    return c;
  };

  result.list.sort((a, b) => {
    // 1. 云盘顺序
    const oa = orderMap[a.pan] ?? 999;
    const ob = orderMap[b.pan] ?? 999;
    if (oa !== ob) return oa - ob;

    // 2. 画质分数
    const qa = getQualityScore(a.vod_name);
    const qb = getQualityScore(b.vod_name);
    if (qa !== qb) return qb - qa;

    // 3. 完结关键词数量
    const ca = getCount(a.vod_name, completedKeywords);
    const cb = getCount(b.vod_name, completedKeywords);
    if (ca !== cb) return cb - ca;

    // 4. 画质关键词数量（次要）
    const qa2 = getCount(a.vod_name, qualityKeywords);
    const qb2 = getCount(b.vod_name, qualityKeywords);
    if (qa2 !== qb2) return qb2 - qa2;

    // 5. 时间倒序（最新在前面）
    if (b.time !== a.time) return b.time - a.time;

    return 0; // 稳定排序
  });

  return result;
};

const play = async ({ flag, flags, id }) => {
  const drive = store.drives.find((o) =>
    new RegExp(`^${o.key}`, "i").test(flag)
  );
  return await drive?.play(id, flag);
};

const cachedFunction = (fn, timeout = 1) => {
  return async (...args) => {
    const name = fn.name.replace("_", "").trim();

    store.log?.info(args);
    const cacheKey = `${store.meta.key}${name}:${CryptoJS.MD5(
      JSON.stringify(args)
    )}`;

    let result;
    result = await store.redis?.get(cacheKey);
    if (result) {
      result = JSON.parse(result);
    } else {
      result = await fn.apply(this, args);
      await store.redis?.set(cacheKey, JSON.stringify(result));
      if ((timeout ?? 0) > 0) {
        await store.redis?.expire(cacheKey, timeout);
      }
    }
    return result;
  };
};

const store = {
  init: false,
  meta: {
    key: "盘搜高码",
    name: "盘搜|高码",
    type: 4,
    api: "/video/盘搜高码",
    searchable: 1,
    quickSearch: 1,
    changeable: 0,
  },
  home: async ({ filter }) => ({
    class: [
      {
        type_id: 1,
        type_name: "纯搜|无资源",
      },
    ],
    list: [],
  }),
  category: async ({ id, page, filter, filters }) => ({
    list: [
      {
        vod_id: 1,
        vod_name: "无资源",
        vod_pic: "",
        vod_remarks: "纯搜",
      },
    ],
    page: 1,
    pagecount: 1,
  }),
  detail: cachedFunction(detail),
  search: cachedFunction(search),
  play: cachedFunction(play),
};

module.exports = async (app, opt) => {
  app.get(store.meta.api, async (req, reply) => {
    if (!store.init) {
      await init(req.server);
    }

    const { extend, filter, t, ac, pg, ext, ids, flag, play, wd, quick } =
      req.query;

    if (play) {
      return await store.play({ flag: flag || "", flags: [], id: play });
    } else if (wd) {
      return await store.search({
        page: parseInt(pg || "1"),
        quick: quick || false,
        wd,
      });
    } else if (!ac) {
      return await store.home({ filter: filter ?? false });
    } else if (ac === "detail") {
      if (t) {
        const body = {
          id: t,
          page: parseInt(pg || "1"),
          filter: filter || false,
          filters: {},
        };
        if (ext) {
          try {
            body.filters = JSON.parse(
              CryptoJS.enc.Base64.parse(ext).toString(CryptoJS.enc.Utf8)
            );
          } catch {}
        }
        return await store.category(body);
      } else if (ids) {
        return await store.detail({
          id: ids
            .split(",")
            .map((_id) => _id.trim())
            .filter(Boolean),
        });
      }
    }

    return req.query;
  });
  opt.sites.push(store.meta);
};