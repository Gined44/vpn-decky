(function () {
  'use strict';

  var index = (function() {
    const { useState, useEffect, useRef } = SP_REACT;
    const {
      ButtonItem,
      PanelSection,
      PanelSectionRow,
      TextField,
      Field,
      staticClasses
    } = DFL;

    function Content({ serverAPI }) {
      const [key, setKey] = useState("");
      const [profiles, setProfiles] = useState([]); // [{name, subscription, type}]
      const [subscriptions, setSubscriptions] = useState([]);
      const [subsInfo, setSubsInfo] = useState([]); // [{name, last_updated}]
      const [activeTab, setActiveTab] = useState("all");
      const [active, setActive] = useState(null);
      const [connected, setConnected] = useState(false);
      const [busy, setBusy] = useState(false);
      const [refreshing, setRefreshing] = useState(false);
      const [pings, setPings] = useState({}); // name -> "42 мс" | "недоступен" | "..."
      const [error, setError] = useState("");
      const [showLogs, setShowLogs] = useState(false);
      const [logLines, setLogLines] = useState([]);
      const [downloadState, setDownloadState] = useState({ downloading: false, progress: 0, done: true, error: null });
      const [netInfo, setNetInfo] = useState(null);
      const mountedRef = useRef(true);

      // silent=true — для фонового polling'а, не трогает спиннер и не дёргает UI.
      // Спиннер показываем только когда юзер сам что-то сделал (добавил/удалил/подключил).
      const refresh = async (silent = false) => {
        if (!silent) setRefreshing(true);
        const profilesRes = await serverAPI.callPluginMethod("list_profiles_full", {});
        if (!mountedRef.current) return;
        if (profilesRes.success) setProfiles(profilesRes.result);
        const subsRes = await serverAPI.callPluginMethod("list_subscriptions", {});
        if (!mountedRef.current) return;
        if (subsRes.success) setSubscriptions(subsRes.result);
        const subsInfoRes = await serverAPI.callPluginMethod("get_subscriptions_info", {});
        if (!mountedRef.current) return;
        if (subsInfoRes.success) setSubsInfo(subsInfoRes.result);
        const statusRes = await serverAPI.callPluginMethod("status", {});
        if (!mountedRef.current) return;
        if (statusRes.success) {
          setConnected(statusRes.result.connected);
          setActive(statusRes.result.active_profile);
        }
        if (!silent) setRefreshing(false);
      };

      useEffect(() => {
        mountedRef.current = true;
        refresh();
        const id = setInterval(() => refresh(true), 3e3);
        return () => {
          mountedRef.current = false;
          clearInterval(id);
        };
      }, []);

      // отдельный опрос статуса скачивания sing-box — пока не done, ничем в UI
      // пользоваться нельзя (см. ранний return ниже); держим всегда, чтобы retry тоже подхватывался
      useEffect(() => {
        let cancelled = false;
        const checkDownload = async () => {
          const res = await serverAPI.callPluginMethod("get_download_status", {});
          if (cancelled) return;
          if (res.success) setDownloadState(res.result);
        };
        checkDownload();
        const id = setInterval(checkDownload, 1e3);
        return () => {
          cancelled = true;
          clearInterval(id);
        };
      }, []);

      // отдельный, более редкий опрос сетевой диагностики — раз в 15 сек, а не каждые 3,
      // чтобы не гонять лишний curl-запрос наружу постоянно, пока панель просто открыта
      useEffect(() => {
        let cancelled = false;
        const checkNetwork = async () => {
          const res = await serverAPI.callPluginMethod("network_info", {});
          if (cancelled) return;
          if (res.success) setNetInfo(res.result);
        };
        checkNetwork();
        const id = setInterval(checkNetwork, 15e3);
        return () => {
          cancelled = true;
          clearInterval(id);
        };
      }, []);
      const handleAdd = async () => {
        if (!key.trim()) return;
        setBusy(true);
        setError("");
        const res = await serverAPI.callPluginMethod("add_profile", { key: key.trim() });
        setBusy(false);
        if (res.success && res.result.ok) {
          setKey("");
          await refresh();
        } else {
          setError(res.success ? (res.result.error || "не удалось добавить") : "ошибка вызова");
        }
      };

      const handleToggle = async (name) => {
        setBusy(true);
        setError("");
        if (connected && active === name) {
          await serverAPI.callPluginMethod("disconnect", {});
        } else {
          const res = await serverAPI.callPluginMethod("connect", { name });
          if (!res.success || !res.result.ok) {
            setError(res.success ? (res.result.error || "не удалось подключиться") : "ошибка вызова");
          }
        }
        setBusy(false);
        await refresh();
      };

      const handleDelete = async (name) => {
        await serverAPI.callPluginMethod("delete_profile", { name });
        await refresh();
      };

      const [subBusy, setSubBusy] = useState(null); // имя подписки, которая сейчас обновляется

      const handleRefreshSubscription = async (name) => {
        setSubBusy(name);
        setError("");
        const res = await serverAPI.callPluginMethod("refresh_subscription", { name });
        setSubBusy(null);
        if (!res.success || !res.result.ok) {
          setError(res.success ? (res.result.error || "не удалось обновить подписку") : "ошибка вызова");
        }
        await refresh();
      };

      const handleDeleteSubscription = async (name) => {
        setBusy(true);
        if (activeTab === name) setActiveTab("all");
        await serverAPI.callPluginMethod("delete_subscription", { name });
        setBusy(false);
        await refresh();
      };

      const loadLogs = async () => {
        const res = await serverAPI.callPluginMethod("get_logs", {});
        if (res.success && res.result.ok) {
          setLogLines(res.result.lines || []);
        }
      };

      const toggleLogs = async () => {
        if (!showLogs) await loadLogs();
        setShowLogs((prev) => !prev);
      };

      // скрытый вход в режим разработчика — 5 быстрых кликов по строке статуса за 3 секунды.
      // Никаких привязок к геймпад-кнопкам — те два раза не сработали надёжно
      // (Options не появлялся, onSecondaryButton дёргал системное "назад").
      // Обычный клик — то, что гарантированно работает везде.
      const devTapRef = useRef({ count: 0, timer: null });
      const handleStatusTap = () => {
        const state = devTapRef.current;
        state.count += 1;
        if (state.timer) clearTimeout(state.timer);
        state.timer = setTimeout(() => { state.count = 0; }, 3000);
        if (state.count >= 5) {
          state.count = 0;
          toggleLogs();
        }
      };

      const handlePing = async (name) => {
        setPings((prev) => ({ ...prev, [name]: "..." }));
        const res = await serverAPI.callPluginMethod("ping_profile", { name });
        if (res.success && res.result.ok) {
          setPings((prev) => ({ ...prev, [name]: `${res.result.ms} мс` }));
        } else {
          setPings((prev) => ({ ...prev, [name]: "недоступен" }));
        }
      };

      const visibleProfiles = activeTab === "all"
        ? profiles
        : profiles.filter((p) => p.subscription === activeTab);

      const formatAgo = (ts) => {
        if (!ts) return null;
        const diffSec = Math.floor(Date.now() / 1000 - ts);
        if (diffSec < 60) return "только что";
        if (diffSec < 3600) return `${Math.floor(diffSec / 60)} мин назад`;
        if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} ч назад`;
        return `${Math.floor(diffSec / 86400)} дн назад`;
      };
      const oldestUpdate = subsInfo.length
        ? Math.min(...subsInfo.map((s) => s.last_updated || Infinity).filter((t) => t !== Infinity))
        : null;

      // пока бинарник sing-box не готов — показываем только прогресс,
      // весь остальной функционал недоступен (нечем подключаться)
      if (downloadState.downloading || (!downloadState.done && !downloadState.error)) {
        return SP_REACT.createElement(
          PanelSection,
          { title: "Первый запуск" },
          SP_REACT.createElement(
            PanelSectionRow,
            null,
            SP_REACT.createElement(
              Field,
              { label: "Скачивается sing-box" },
              `${downloadState.progress || 0}%`
            )
          ),
          SP_REACT.createElement(
            PanelSectionRow,
            null,
            SP_REACT.createElement(
              "div",
              {
                style: {
                  width: "100%",
                  height: "6px",
                  background: "rgba(255,255,255,0.15)",
                  borderRadius: "3px",
                  overflow: "hidden",
                },
              },
              SP_REACT.createElement("div", {
                style: {
                  width: `${downloadState.progress || 0}%`,
                  height: "100%",
                  background: "#5c9df1",
                  transition: "width 0.3s ease",
                },
              })
            )
          ),
          SP_REACT.createElement(
            PanelSectionRow,
            null,
            SP_REACT.createElement(Field, { label: "Подожди немного, это разово" })
          )
        );
      }

      if (downloadState.error) {
        return SP_REACT.createElement(
          PanelSection,
          { title: "Ошибка загрузки" },
          SP_REACT.createElement(
            PanelSectionRow,
            null,
            SP_REACT.createElement(Field, { label: "Не удалось скачать sing-box" }, downloadState.error)
          ),
          SP_REACT.createElement(
            PanelSectionRow,
            null,
            SP_REACT.createElement(
              ButtonItem,
              { layout: "below", onClick: async () => { await serverAPI.callPluginMethod("retry_download", {}); } },
              "Попробовать снова"
            )
          )
        );
      }

      return SP_REACT.createElement(
        SP_REACT.Fragment,
        null,
        SP_REACT.createElement(
          PanelSection,
          { title: "Добавить ключ" },
          SP_REACT.createElement(
            PanelSectionRow,
            null,
            SP_REACT.createElement(TextField, {
              value: key,
              onChange: (e) => setKey(e.target.value),
              label: "vless://, hysteria2:// или ссылка на подписку"
            })
          ),
          SP_REACT.createElement(
            PanelSectionRow,
            null,
            SP_REACT.createElement(
              ButtonItem,
              {
                layout: "below",
                disabled: busy,
                onClick: handleAdd,
              },
              "Добавить профиль"
            )
          ),
          error && SP_REACT.createElement(
            PanelSectionRow,
            null,
            SP_REACT.createElement(Field, { label: "Ошибка" }, error)
          )
        ),
        subscriptions.length > 1 && SP_REACT.createElement(
          PanelSection,
          { title: "Подписки" },
          SP_REACT.createElement(
            PanelSectionRow,
            null,
            SP_REACT.createElement(
              ButtonItem,
              { layout: "below", disabled: busy, onClick: () => setActiveTab("all") },
              activeTab === "all" ? "\u2713 Все" : "Все"
            )
          ),
          ...subscriptions.map((sub) =>
            SP_REACT.createElement(
              PanelSectionRow,
              { key: sub },
              SP_REACT.createElement(
                ButtonItem,
                { layout: "below", disabled: busy, onClick: () => setActiveTab(sub) },
                subBusy === sub
                  ? `${sub} — обновляю...`
                  : activeTab === sub ? `\u2713 ${sub}` : sub
              )
            )
          ),
          // управление показываем только для выбранной конкретной подписки,
          // чтобы не плодить кучу кнопок и не ломать вёрстку рядом сжатыми элементами
          activeTab !== "all" && SP_REACT.createElement(
            PanelSectionRow,
            null,
            SP_REACT.createElement(
              ButtonItem,
              { layout: "below", disabled: busy || subBusy === activeTab, onClick: () => handleRefreshSubscription(activeTab) },
              subBusy === activeTab ? "Обновляю..." : `Обновить ${activeTab}`
            ),
            SP_REACT.createElement(
              ButtonItem,
              { layout: "below", disabled: busy, onClick: () => handleDeleteSubscription(activeTab) },
              `Удалить подписку ${activeTab}`
            )
          ),
          oldestUpdate && SP_REACT.createElement(
            PanelSectionRow,
            null,
            SP_REACT.createElement(
              "span",
              { style: { fontSize: "11px", opacity: 0.5 } },
              `Обновлено: ${formatAgo(oldestUpdate)}`
            )
          )
        ),
        SP_REACT.createElement(
          PanelSection,
          { title: "Профили", spinner: refreshing },
          visibleProfiles.length === 0 && SP_REACT.createElement(
            PanelSectionRow,
            null,
            SP_REACT.createElement(Field, { label: "Пусто. Вставь ключ выше." })
          ),
          ...visibleProfiles.map(
            (p) => SP_REACT.createElement(
              SP_REACT.Fragment,
              { key: p.name },
              SP_REACT.createElement(
                PanelSectionRow,
                null,
                SP_REACT.createElement(
                  Field,
                  { label: p.display_name || p.name },
                  SP_REACT.createElement(
                    "span",
                    { style: { fontSize: "11px", opacity: 0.6, textTransform: "uppercase" } },
                    p.type
                  ),
                  // если имя получило суффикс (-2, -3...) из-за коллизии с другой подпиской —
                  // показываем откуда этот профиль, чтобы не путать с одноимённым из другой подписки
                  p.name !== p.display_name && SP_REACT.createElement(
                    "div",
                    { style: { fontSize: "10px", opacity: 0.5 } },
                    `из: ${p.subscription}`
                  )
                )
              ),
              SP_REACT.createElement(
                PanelSectionRow,
                null,
                SP_REACT.createElement(
                  ButtonItem,
                  { layout: "below", disabled: busy, onClick: () => handleToggle(p.name) },
                  connected && active === p.name ? `\u23F9 Отключить` : `\u25B6 Подключить`
                )
              ),
              SP_REACT.createElement(
                PanelSectionRow,
                null,
                SP_REACT.createElement(
                  ButtonItem,
                  { layout: "below", disabled: busy, onClick: () => handlePing(p.name) },
                  pings[p.name] ? `Пинг: ${pings[p.name]}` : "Пинг"
                ),
                SP_REACT.createElement(
                  ButtonItem,
                  { layout: "below", disabled: busy, onClick: () => handleDelete(p.name) },
                  "Удалить"
                )
              )
            )
          )
        ),
        SP_REACT.createElement(
          PanelSection,
          { title: "Статус" },
          SP_REACT.createElement(
            PanelSectionRow,
            null,
            SP_REACT.createElement(
              Field,
              { label: "Соединение", focusable: true },
              connected ? `\u{1F7E2} подключено${active ? " (" + active + ")" : ""}` : "\u{1F534} отключено"
            )
          ),
          netInfo && SP_REACT.createElement(
            PanelSectionRow,
            null,
            SP_REACT.createElement(
              Field,
              { label: "Интерфейс" },
              netInfo.iface || "неизвестен"
            )
          ),
          netInfo && SP_REACT.createElement(
            PanelSectionRow,
            null,
            SP_REACT.createElement(
              Field,
              { label: "tun-decky" },
              netInfo.tun_up ? "\u{1F7E2} поднят" : "\u{1F534} не поднят"
            )
          ),
          netInfo && SP_REACT.createElement(
            PanelSectionRow,
            null,
            SP_REACT.createElement(
              Field,
              { label: "Интернет" },
              netInfo.internet_ok ? "\u{1F7E2} есть" : "\u{1F534} нет"
            )
          ),
          netInfo && netInfo.server_ok !== null && SP_REACT.createElement(
            PanelSectionRow,
            null,
            SP_REACT.createElement(
              Field,
              { label: "Сервер VPN" },
              netInfo.server_ok ? "\u{1F7E2} отвечает" : "\u{1F534} не отвечает"
            )
          ),
          SP_REACT.createElement(
            PanelSectionRow,
            null,
            SP_REACT.createElement(
              ButtonItem,
              { layout: "below", onClick: handleStatusTap },
              SP_REACT.createElement(
                "span",
                { style: { fontSize: "10px", opacity: 0.35 } },
                "vpn-decky"
              )
            )
          )
        ),
        // видно только после 5 быстрых кликов по мелкой подписи "vpn-decky" внизу статуса
        showLogs && SP_REACT.createElement(
          PanelSection,
          { title: "Логи sing-box (режим разработчика)" },
          SP_REACT.createElement(
            PanelSectionRow,
            null,
            SP_REACT.createElement(
              ButtonItem,
              { layout: "below", onClick: loadLogs },
              "Обновить лог"
            )
          ),
          SP_REACT.createElement(
            PanelSectionRow,
            null,
            SP_REACT.createElement(
              "div",
              {
                style: {
                  fontSize: "10px",
                  fontFamily: "monospace",
                  whiteSpace: "pre-wrap",
                  maxHeight: "300px",
                  overflowY: "auto",
                  opacity: 0.8,
                },
              },
              logLines.length ? logLines.join("\n") : "лог пуст"
            )
          )
        )
      );
    }
    return function(serverAPI) {
      return {
        title: SP_REACT.createElement("div", { className: DFL.staticClasses.Title }, "VPN"),
        content: SP_REACT.createElement(Content, { serverAPI }),
        icon: SP_REACT.createElement("div", null, "\u{1F6E1}\uFE0F"),
        onDismount() {
        }
      };
    };
  })();

  return index;

})();
