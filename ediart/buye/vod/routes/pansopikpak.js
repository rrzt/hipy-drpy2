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
  	pikpak: "pikpak",
  };

  const res = await _http.post("/api/search", {
    kw: wd,
    cloud_types: store.drives.map((d) => panTypes[d.key]).filter(Boolean),
    // ext: { referer: "https://dm.xueximeng.com" }, // 扩展参数，传递给插件的自定义参数
  });
  const ret = res.data;
  if (ret.code !== 0) {
    throw new Error(ret.message || "请求失败");
  }

  for (const key in ret.data.merged_by_type || {}) {
    const value = Object.keys(panTypes).find((k) => panTypes[k] === key);
    const drive = store.drives.find((o) => o.key === value);
    const pic = drive?.pic || "";

    for (const row of ret.data.merged_by_type[key] || []) {
      const currentTime = dayjs();
      const rowTime = dayjs(row.datetime);
      const timeDiff = currentTime.diff(rowTime, 'minute');
      
      // 使用条件运算符简化代码
      const dt = dayjs(timeDiff <= 70 && timeDiff >= 0 ? "0001-01-01T00:00:00Z" : row.datetime);
      const source = row.source ? row.source.replace(/plugin:/gi, 'plg:') : "";

      result.list.push({
        vod_id: row.url,
        vod_name: row.note,
        vod_pic: (row.images?.[0]||"").trim() || pic || "",
        vod_remarks: `${source || ""} | ${dt.format("MMDDYY")}`,
        time: dt.unix(),
        pan: value,
      });
    }
  }

  result.list.sort((a, b) => b.time - a.time); // 排序，最新的在前面
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
    key: "pansopikpak",
    name: "盘搜|PikPak",
    type: 4,
    api: "/video/pansopikpak",
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
