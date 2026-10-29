// テーマ切替(localStorage 永続)と、破壊的/不可逆な操作の確認ダイアログ。
(function () {
  "use strict";

  // --- テーマ切替 ---
  var root = document.documentElement;
  var btn = document.getElementById("theme-toggle");

  if (btn) {
    btn.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("xnews-theme", next); } catch (e) {}
    });
  }

  // --- 確認ダイアログ(誤操作・誤上書き防止) ---
  document.querySelectorAll("form.needs-confirm").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      var msg = form.getAttribute("data-confirm") || "実行しますか？";
      var input = form.querySelector('input[type="time"]');
      if (input && input.value) msg += "\n\n新しい時刻: " + input.value;
      if (!window.confirm(msg)) e.preventDefault();
    });
  });

  // --- AI解説(押されたときだけ作る。作成中は経過秒を出しつつ状態を見に行く) ---
  function explainBox(box) {
    var id = box.getAttribute("data-explain-id");
    var btn = box.querySelector(".explain-btn");
    var status = box.querySelector(".explain-status");
    var out = box.querySelector(".explain-out");
    if (!id || !btn || !status || !out) return;
    var started = 0, tick = null, poll = null;

    function stop() {
      clearInterval(tick); clearInterval(poll);
      tick = poll = null;
    }
    function showRunning() {
      var sec = Math.max(0, Math.round((Date.now() - started) / 1000));
      status.classList.remove("is-error");
      status.textContent = "AI解説を作成中… 経過" + sec + "秒（1〜2分かかります）";
      status.hidden = false;
    }
    // msg: サーバーが返した理由(受け付けなかった理由・失敗の理由)。無ければ決まり文句
    function fail(msg) {
      stop();
      status.classList.add("is-error");
      status.textContent = (typeof msg === "string" && msg) ? msg : "作れませんでした。もう一度押せます";
      status.hidden = false;
      btn.hidden = false;
    }
    function apply(st) {
      if (st.status === "done") {
        stop();
        out.textContent = st.text;  // 外部由来の文章なので innerHTML は使わない
        out.hidden = false;
        status.hidden = true;
        btn.hidden = true;
      } else if (st.status === "running") {
        btn.hidden = true;
        if (!tick) {
          started = Date.now() - (st.elapsed || 0) * 1000;
          showRunning();
          tick = setInterval(showRunning, 1000);
          poll = setInterval(check, 3000);
        }
      } else {
        fail(st.error);
      }
    }
    function request(method) {
      return fetch("/explain/" + encodeURIComponent(id), {
        method: method, credentials: "same-origin", headers: { "Accept": "application/json" }
      }).then(function (r) {
        if (!r.ok) {
          var err = new Error("HTTP " + r.status);
          err.status = r.status;
          throw err;
        }
        return r.json();
      });
    }
    function failRequest(e) {
      fail(e && e.status === 401 ? "ログインし直してください" : "");
    }
    function check() {
      // 一時的な通信失敗は次のポーリングでやり直す(ログインが切れたときだけ止めて知らせる)
      request("GET").then(apply).catch(function (e) {
        if (e && e.status === 401) failRequest(e);
      });
    }

    btn.addEventListener("click", function () {
      btn.hidden = true;
      started = Date.now();
      showRunning();
      request("POST").then(apply).catch(failRequest);
    });
  }
  document.querySelectorAll(".explain[data-explain-id]").forEach(explainBox);

  // --- 評価(興味あり/興味なし)。押した方を aria-pressed で示す。失敗したら元の状態に戻す ---
  document.querySelectorAll(".feedback[data-feedback-id]").forEach(function (box) {
    var id = box.getAttribute("data-feedback-id");
    var hash = box.getAttribute("data-feedback-hash");
    var btns = box.querySelectorAll(".fb-btn");
    var STALE_MSG = "記事が入れ替わりました。ページを読み込み直してください";
    var note = document.createElement("p");  // AI解説と同じエラー表示(.explain-status.is-error)を流用
    note.className = "explain-status is-error";
    note.setAttribute("role", "status");
    note.hidden = true;
    box.parentNode.appendChild(note);
    function show(verdict) {
      btns.forEach(function (b) {
        b.setAttribute("aria-pressed", b.getAttribute("data-verdict") === verdict ? "true" : "false");
      });
    }
    btns.forEach(function (b) {
      b.addEventListener("click", function () {
        var prev = box.querySelector('.fb-btn[aria-pressed="true"]');
        var verdict = b.getAttribute("data-verdict");
        show(verdict);
        fetch("/feedback/" + encodeURIComponent(id) + "/" + encodeURIComponent(hash) + "/" + verdict, {
          method: "POST", credentials: "same-origin", headers: { "Accept": "application/json" }
        }).then(function (r) {
          if (r.status === 409) {
            // 取り込み直しで記事が入れ替わった。理由を数秒出して読み込み直しを促す
            return r.json().catch(function () { return {}; }).then(function (j) {
              throw new Error((j && typeof j.detail === "string" && j.detail) || STALE_MSG);
            });
          }
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.json();
        }).then(function (st) { show(st.verdict); }).catch(function (e) {
          show(prev ? prev.getAttribute("data-verdict") : "");
          var stale = e && e.message && e.message.indexOf("HTTP ") !== 0 ? e.message : "";
          box.classList.add("is-error");
          if (stale) { note.textContent = stale; note.hidden = false; }
          setTimeout(function () {
            box.classList.remove("is-error");
            note.hidden = true;
          }, stale ? 5000 : 1500);
        });
      });
    });
  });
})();
