// Session Memory Terminal Management
// Auto-loaded by dashboard index.html

function loadTerminals() {
  fetch("/api/v1/terminals/online").then(r => r.json()).then(data => {
    document.getElementById("onlineCount").textContent = data.length;
    var h = "";
    data.forEach(function(t) {
      h += '<div style="display:flex;justify-content:space-between;padding:8px;background:#f0fdf4;border-radius:8px;margin-bottom:6px">' +
           '<div><span style="font-size:20px">🟢</span> <b>' + t.terminal_name + '</b><br>' +
           '<small class="text-gray">' + (t.hostname||"") + ' · ' + (t.os_info||"") + '</small></div>' +
           '<small class="text-gray">' + (t.last_seen||"").slice(11,19) + '</small></div>';
    });
    document.getElementById("onlineTerminalList").innerHTML = h || '<span class="text-gray">暂无在线终端</span>';
    document.getElementById("overviewTerminals").innerHTML = '<b class="text-green">' + data.length + '</b> 个终端在线';
  }).catch(function(){
    document.getElementById("onlineTerminalList").innerHTML = '<span class="text-red">连接失败</span>';
    document.getElementById("overviewTerminals").innerHTML = '<span class="text-red">连接失败</span>';
  });
  fetch("/api/v1/terminals").then(r => r.json()).then(all => {
    var on = all.filter(t => t.is_online).length;
    document.getElementById("overviewTerminals").innerHTML = '<b class="text-green">' + on + '</b>/<b>' + all.length + '</b> 在线';
  }).catch(function(){});
}

function loadTerminalMgmt() {
  var el = document.getElementById("terminalMgmtList");
  el.innerHTML = '<span class="text-gray">加载中...</span>';
  fetch("/api/v1/terminals").then(r => r.json()).then(data => {
    var h = '<table><tr><th>终端</th><th>主机</th><th>OS</th><th>在线</th><th>会话</th><th>操作</th></tr>';
    data.forEach(function(t) {
      var dot = t.is_online ? '<span class="text-green">●</span>' : '<span class="text-red">●</span>';
      h += '<tr>' +
           '<td><b>' + t.terminal_name + '</b></td>' +
           '<td>' + (t.hostname||"-") + '</td>' +
           '<td>' + (t.os_info||"-") + '</td>' +
           '<td style="text-align:center">' + dot + '</td>' +
           '<td style="text-align:center">' + (t.session_count||0) + '</td>' +
           '<td><button class="btn btn-gray" onclick="showShareSessions(\'' + t.id + '\',\'' + t.terminal_name + '\')">共享设置</button></td>' +
           '</tr>';
    });
    h += '</table>';
    el.innerHTML = h;
  }).catch(function(){ el.innerHTML = '<span class="text-red">加载失败</span>'; });
}

function showShareSessions(tid, name) {
  var p = document.getElementById("shareMgmtPanel");
  p.innerHTML = '<p class="text-xs text-blue-600">加载 ' + name + ' 的会话列表...</p>';
  fetch("/api/v1/terminals/" + tid + "/sessions").then(r => r.json()).then(shares => {
    fetch("/api/v1/sessions?limit=30").then(r => r.json()).then(sessions => {
      var h = '<b>' + name + '</b><br><small class="text-gray">勾选=共享, 不勾=独享</small><div style="margin-top:8px;max-height:300px;overflow-y:auto">';
      sessions.forEach(function(s) {
        var share = shares.find(function(x){ return x.id === s.id; });
        var ck = share && share.can_share ? "checked" : "";
        h += '<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid #f1f5f9">' +
             '<span style="font-size:12px;max-width:200px;overflow:hidden;text-overflow:ellipsis">' + (s.title||"untitled") + '</span>' +
             '<label style="cursor:pointer;display:flex;align-items:center;gap:4px;font-size:12px">' +
             '<input type="checkbox" ' + ck + ' onchange="toggleShare(\'' + tid + '\',\'' + s.id + '\',this.checked)"> 共享</label></div>';
      });
      h += '</div>';
      p.innerHTML = h;
    }).catch(function(){ p.innerHTML = '<span class="text-red">加载失败</span>'; });
  }).catch(function(){ p.innerHTML = '<span class="text-red">加载失败</span>'; });
}

function toggleShare(tid, sid, can) {
  fetch("/api/v1/terminals/" + tid + "/share", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({session_id: sid, can_share: can})
  }).then(r => r.json()).then(d => {
    if (d.status !== "ok") alert("更新失败");
  }).catch(e => alert("更新失败: " + e.message));
}

// Hook: refresh when terminal tab is shown
setInterval(function(){
  var tab = document.getElementById("tab-terminals");
  if (tab && tab.classList.contains("active")) loadTerminals();
}, 30000);

console.log("[Terminals] Management module loaded");
