package gateway

import "strings"

// BridgeScript 返回注入 HTML 的运行时桥接脚本。
//
// 为什么必须有它：正则只能改写静态 HTML，JS 运行时拼接的 URL（fetch、XHR、
// 动态插入的 iframe 等）不经过 HTML，必须靠脚本在运行时拦截。
// 这是网关适配中最容易漏掉、也最容易导致白屏的一环（见 skill
// references/gateway-proxy.md §4）。本脚本承担三件事：
//
//  1. 前缀改写：所有同源 URL 补上 /app/agent2api，已带前缀的不重复加。
//  2. 鉴权头迁移：Authorization: Bearer <key> → x-api-key。
//     飞牛统一网关（1.2.0604+）会把应用自己的 Authorization 当成非法飞牛
//     票据直接拦截（invalid token），而 agent2api 的 require_api_key 同时
//     接受 Authorization 与 x-api-key（server/src/server/access.rs），
//     因此这层迁移是必需的。
//  3. 整页跳转兜底：location.assign / location.replace 的站内绝对路径补前缀。
//
// 为什么 agent2api 不需要 cli2api 那套「构建期 router 补丁」：
// 实测面板产物（desktop-tauri/ui）**没有任何前端路由** —— islands/ui.js 与
// app.js 里 location.pathname / history.pushState / createBrowserRouter 的出现
// 次数均为 0，页面切换靠内存里的 currentPage 变量 + CSS class。因此不存在
// 「读侧拿不到前缀」的问题，运行时改写就足以覆盖全部动态 URL。
//
// 安全约束（同 skill 要求）：
//   - 同源检查：只改写同源 URL，外链/CDN 不动。
//   - 幂等检查：已带前缀不重复加，避免 /app/agent2api/app/agent2api/...。
//   - 协议白名单：只处理 http/https/ws/wss。
//   - 全程 try/catch：桥接脚本自身报错会让整页白屏，异常必须吞掉。
//   - 全局标记防重复安装。
//
// 该脚本只会经 ${TRIM_APPDEST}/agent2api.sock 注入到 /app/agent2api 前缀下的
// HTML，因此可以假定页面本身位于前缀之下。
func BridgeScript(prefix Prefix) string {
	var b strings.Builder
	b.WriteString(`<script data-fn-gateway-bridge="1">(function(){`)
	b.WriteString(`if(window.__fnGatewayBridgeReady)return;window.__fnGatewayBridgeReady=1;`)
	b.WriteString(`var P=`)
	b.WriteString(jsString(prefix.Path))
	b.WriteString(`;window.__fnGatewayBase=P;`)
	b.WriteString(bridgeBody)
	b.WriteString(`})();</script>`)
	return b.String()
}

// SeedScript 给面板的密钥槽位放一个占位值。
//
// agent2api 的 Web 壳（server/src/web_shim.rs，编译进二进制）在脚本顶层把
// localStorage['agent2api.webKey'] 读进闭包变量 apiKey，之后每个 /api 请求
// 都带上 `x-api-key: apiKey`；缺失时会弹「需要网关 API Key」输入层。
// 真实密钥由网关在服务端注入（见 main.go 的 Prepare 钩子），**不下发到浏览器**；
// 这里只放一个占位值，让面板不必弹框、也不必读用户输入。
//
// 本脚本由 InjectHead 插到 <head> 之后，即**先于**服务端注入的那段 Web 壳执行，
// 因此这里写入的值一定能被它读到。
func SeedScript() string {
	return `<script data-fn-gateway-seed="1">(function(){try{` +
		`var K="agent2api.webKey",S="fnos-gateway-session",ls=window.localStorage;` +
		`ls.setItem(K,S);` +
		`var set=ls.setItem,rm=ls.removeItem;` +
		`ls.setItem=function(k,v){if(k===K&&!String(v==null?"":v).trim()){v=S}return set.call(ls,k,v)};` +
		`ls.removeItem=function(k){if(k===K){return set.call(ls,K,S)}return rm.call(ls,k)};` +
		`}catch(e){}})();</script>`
}

// jsString 安全地把 Go 字符串编码为 JS 字面量（防注入/防提前闭合 script）。
func jsString(s string) string {
	// 只允许网关前缀这种受控字符集；任何异常字符一律 unicode 转义。
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9':
			b.WriteRune(r)
		case r == '/', r == '-', r == '_', r == '.', r == '~':
			b.WriteRune(r)
		default:
			const hex = "0123456789abcdef"
			b.WriteString(`\u`)
			b.WriteByte(hex[(r>>12)&0xF])
			b.WriteByte(hex[(r>>8)&0xF])
			b.WriteByte(hex[(r>>4)&0xF])
			b.WriteByte(hex[r&0xF])
		}
	}
	b.WriteByte('"')
	return b.String()
}

// bridgeBody 桥接脚本主体（纯 JS，不含 Go 模板；注意不能出现反引号，
// 因为它被写成 Go 原始字符串）。
const bridgeBody = `
function safe(fn){try{return fn()}catch(e){return null}}
function already(p){return p===P||p.indexOf(P+'/')===0}
/* 解析基准必须显式带上尾斜杠。
   飞牛桌面入口的 url 是 /app/agent2api（不带尾斜杠），文档 URL 就是
   https://host/app/agent2api。此时把裸相对引用（assets/providers/x.png）
   按 window.location.href 解析会退到上一级 /app/assets/...，再补前缀就变成
   /app/agent2api/app/assets/...，必然 404 —— 这正是「添加账号 → 反代」里
   模型图标全部不显示的原因。
   用 P+'/' 作基准后，裸相对引用解析结果天然落在前缀之下，already() 判定为
   「已带前缀」直接放行，由浏览器按 <base> 自行解析，桥接不再插手改坏它。
   new URL 的第二个参数必须是**绝对** URL，所以这里用 P+'/' 相对当前文档
   求一次绝对值；万一环境异常求不出来，退回原来的 href（行为不劣于改前）。 */
var RESOLVE_BASE=safe(function(){return new URL(P+'/',window.location.href).toString()})||window.location.href;
function toGw(v){
  if(v===null||v===undefined||v==='')return null;
  var str=String(v).trim();
  if(/^(blob:|data:|javascript:|about:|#)/i.test(str))return null;
  var u; try{u=new URL(str,RESOLVE_BASE)}catch(e){return null}
  if(!/^(https?|wss?):$/.test(u.protocol))return null;
  if(u.origin!==window.location.origin)return null;
  if(already(u.pathname))return null;
  u.pathname=P+(u.pathname.charAt(0)==='/'?u.pathname:'/'+u.pathname);
  return u;
}
function str(url){var u=toGw(url);return u?u.toString():null}

/* ---- 鉴权头迁移：Authorization -> x-api-key（飞牛网关拦截 Authorization）---- */
function bareSecret(v){
  var s=String(v===null||v===undefined?'':v).trim();
  return s.replace(/^Bearer\s+/i,'');
}
function migrateHeaders(h){
  var out=safe(function(){
    if(!h)return null;
    if(typeof Headers!=='undefined'&&h instanceof Headers){
      var v=h.get('authorization');
      if(v){h.delete('authorization');if(!h.get('x-api-key'))h.set('x-api-key',bareSecret(v));}
      return h;
    }
    var nh=new Headers();
    if(typeof h.forEach==='function'&&!Array.isArray(h)){h.forEach(function(val,key){nh.append(key,val)});}
    else if(Array.isArray(h)){h.forEach(function(pair){if(pair&&pair.length===2)nh.append(pair[0],pair[1])});}
    else if(typeof h==='object'){Object.keys(h).forEach(function(k){nh.append(k,h[k])});}
    else return null;
    var raw=nh.get('authorization');
    if(raw){nh.delete('authorization');if(!nh.get('x-api-key'))nh.set('x-api-key',bareSecret(raw));}
    return nh;
  });
  return out||h;
}

/* fetch */
if(window.fetch){
  var _f=window.fetch;
  window.fetch=function(input,init){
    safe(function(){
      if(typeof input==='string'){
        var s=str(input);if(s)input=s;
        if(init&&init.headers)init.headers=migrateHeaders(init.headers);
        return;
      }
      if(input&&input.url){
        /* Request 对象：url 与 headers 只读，必须重建（保留原方法与头体）。 */
        var s2=str(input.url);
        var h2=input.headers?migrateHeaders(input.headers):null;
        if(s2||h2){
          try{
            input=new Request(s2||input.url,{
              method:input.method,headers:h2||input.headers,body:input.body,
              mode:input.mode,credentials:input.credentials,cache:input.cache,
              redirect:input.redirect,referrer:input.referrer,
              referrerPolicy:input.referrerPolicy,integrity:input.integrity,
              keepalive:input.keepalive,signal:input.signal,duplex:input.duplex
            });
          }catch(e){ /* body 已被消费等情况：退回原始对象，不阻断请求 */ }
        }
        return;
      }
      if(init&&init.headers)init.headers=migrateHeaders(init.headers);
    });
    return _f.call(window,input,init);
  };
}

/* XMLHttpRequest：路径加前缀 + 鉴权头迁移 */
if(window.XMLHttpRequest){
  var _o=XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open=function(m,u){
    var s=safe(function(){return str(u)});
    if(s)arguments[1]=s;
    return _o.apply(this,arguments);
  };
  if(XMLHttpRequest.prototype.setRequestHeader){
    var _srh=XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.setRequestHeader=function(name,value){
      if(typeof name==='string'&&/^authorization$/i.test(name)){
        return _srh.call(this,'x-api-key',bareSecret(value));
      }
      return _srh.call(this,name,value);
    };
  }
}

/* WebSocket：只改写同源同端口的 URL */
if(window.WebSocket){
  var _W=window.WebSocket;
  var W=function(url,protocols){
    var fixed=safe(function(){
      var u=new URL(String(url),RESOLVE_BASE);
      var wp=(window.location.protocol==='https:'?'wss:':'ws:');
      if(u.protocol!==wp)return null;
      if(u.hostname!==window.location.hostname)return null;
      if(u.port!==window.location.port)return null;
      if(already(u.pathname))return null;
      u.pathname=P+(u.pathname.charAt(0)==='/'?u.pathname:'/'+u.pathname);
      return u.toString();
    });
    return protocols===undefined?new _W(fixed||url):new _W(fixed||url,protocols);
  };
  W.prototype=_W.prototype;
  ['CONNECTING','OPEN','CLOSING','CLOSED'].forEach(function(k){try{W[k]=_W[k]}catch(e){}});
  window.WebSocket=W;
}

/* EventSource */
if(window.EventSource){
  var _E=window.EventSource;
  var E=function(url,cfg){
    var fixed=safe(function(){return str(url)});
    return cfg===undefined?new _E(fixed||url):new _E(fixed||url,cfg);
  };
  E.prototype=_E.prototype;
  window.EventSource=E;
}

/* innerHTML / insertAdjacentHTML：只做字符串级属性替换，不执行内容 */
function rewriteHtml(strHtml){
  return safe(function(){
    if(typeof strHtml!=='string'||strHtml.indexOf('/')<0)return strHtml;
    return strHtml.replace(/(\b(?:src|href|action|poster)\s*=\s*["'])(\/(?!\/)[^"']*)/gi,
      function(m,p1,p2){
        if(already(p2))return m;
        return p1+P+p2;
      });
  })||strHtml;
}
try{
  var _ih=Object.getOwnPropertyDescriptor(Element.prototype,'innerHTML');
  if(_ih&&_ih.set){
    var oldIH=_ih.set;
    Object.defineProperty(Element.prototype,'innerHTML',{
      configurable:true,enumerable:_ih.enumerable,get:_ih.get,
      set:function(v){return oldIH.call(this,rewriteHtml(v))}
    });
  }
}catch(e){}
try{
  var _iah=Element.prototype.insertAdjacentHTML;
  Element.prototype.insertAdjacentHTML=function(pos,html){
    return _iah.call(this,pos,rewriteHtml(html));
  };
}catch(e){}

/* Worker 脚本路径 */
try{
  if(window.Worker){
    var _K=window.Worker;
    var K=function(url,opts){
      var fixed=safe(function(){return str(url)});
      return opts===undefined?new _K(fixed||url):new _K(fixed||url,opts);
    };
    K.prototype=_K.prototype;
    window.Worker=K;
  }
}catch(e){}

/* 读侧前缀剥离：agent2api 面板没有任何前端路由（不读 location.pathname、
   不写 history），因此不存在 cli2api 那种「读侧拿不到前缀」的问题，这里
   不需要任何 Location 原型补丁（Chrome 的 Location 属性是
   [LegacyUnforgeable]，本来也打不上）。 */

/* history API：本版面板不写 history，这里只为将来加了路由的版本兜底，
   写入时补前缀，保证 URL 栏可刷新、可深链。 */
try{
  var _ps=history.pushState,_rs=history.replaceState;
  function fixState(u){
    return safe(function(){
      if(u===null||u===undefined)return u;
      var s=str(u);return s||u;
    })||u;
  }
  history.pushState=function(st,t,u){return _ps.call(this,st,t,fixState(u))};
  history.replaceState=function(st,t,u){return _rs.call(this,st,t,fixState(u))};
}catch(e){}

/* location.assign / replace：站内绝对路径补前缀。
   服务端注入的 Web 壳里写死了 window.location.href='/login'（见
   web_shim.rs 的 401 分支）—— 赋值给 location.href 会走 location.assign，
   浏览器拦截不到 href 的 setter（[LegacyUnforgeable]），只能拦 assign/replace。
   实际部署下这条路径不会触发（密钥由服务端注入，不会 401），这里只作兜底。 */
try{
  var _as=window.location.assign,_rp=window.location.replace;
  window.location.assign=function(u){
    var s=safe(function(){return str(u)});
    return _as.call(window.location,s||u);
  };
  window.location.replace=function(u){
    var s=safe(function(){return str(u)});
    return _rp.call(window.location,s||u);
  };
}catch(e){}

/* Element.setAttribute */
try{
  var _sa=Element.prototype.setAttribute;
  Element.prototype.setAttribute=function(n,v){
    if(/^(src|href|action|poster|data)$/i.test(n)){
      var s=safe(function(){return str(v)});if(s)v=s;
    }
    return _sa.call(this,n,v);
  };
}catch(e){}

/* 属性 setter 劫持 */
var PROPS=[['HTMLImageElement','src'],['HTMLImageElement','srcset'],['HTMLLinkElement','href'],
  ['HTMLScriptElement','src'],['HTMLIFrameElement','src'],['HTMLMediaElement','src'],
  ['HTMLVideoElement','poster'],['HTMLSourceElement','src'],['HTMLSourceElement','srcset'],
  ['HTMLFormElement','action'],['HTMLObjectElement','data'],['HTMLEmbedElement','src']];
PROPS.forEach(function(pair){
  try{
    var Ctor=window[pair[0]];if(!Ctor)return;
    var d=Object.getOwnPropertyDescriptor(Ctor.prototype,pair[1]);if(!d||!d.set)return;
    var oldSet=d.set;
    Object.defineProperty(Ctor.prototype,pair[1],{
      configurable:true,enumerable:d.enumerable,
      get:d.get,
      set:function(v){
        var s=safe(function(){return str(v)});
        return oldSet.call(this,s||v);
      }
    });
  }catch(e){}
});

/* 整页跳转兜底（<a> 点击，冒泡阶段）。
   面板没有路由接管 <a>，裸 <a href="/xxx"> 补前缀后整页跳转，
   避免落到飞牛网关的 404 上。 */
document.addEventListener('click',function(ev){
  if(ev.defaultPrevented)return;
  var a=safe(function(){
    var el=ev.target;
    while(el&&el.tagName!=='A')el=el.parentElement;
    return el;
  });
  if(!a||!a.getAttribute)return;
  var href=a.getAttribute('href');
  if(!href||href.charAt(0)==='#'||/^[a-z]+:/i.test(href))return;
  if(a.target&&a.target!=='_self')return;
  var s=safe(function(){return str(href)});
  if(s)ev.preventDefault(),window.location.assign(s);
},false);

/* 动态插入的 iframe 递归安装桥接 */
safe(function(){
  if(!window.MutationObserver)return;
  new MutationObserver(function(muts){
    muts.forEach(function(m){
      Array.prototype.forEach.call(m.addedNodes||[],function(n){
        if(n&&n.tagName==='IFRAME'){safe(function(){
          n.addEventListener('load',function(){safe(function(){
            try{if(n.contentWindow&&!n.contentWindow.__fnGatewayBridgeReady){
              n.contentWindow.__fnGatewayBridgeReady=1;
            }}catch(e){}
          })});
        })}
      });
    });
  }).observe(document.documentElement,{childList:true,subtree:true});
});
`
