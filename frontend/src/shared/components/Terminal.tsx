import { useEffect, useRef, useState } from 'react';
import { Terminal as XTerm } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';

type Props={sessionId:string; enabled:boolean; command:{id:number;text:string}|null; onConnection:(connected:boolean)=>void};
export function TerminalPane({sessionId,enabled,command,onConnection}:Props) {
  const container=useRef<HTMLDivElement>(null);
  const socket=useRef<WebSocket|null>(null);
  const [attempt,setAttempt]=useState(0);
  const [connected,setConnected]=useState(false);
  useEffect(()=>{
    if(!container.current||!enabled) {onConnection(false);setConnected(false);return;}
    const terminal=new XTerm({cursorBlink:true,fontSize:13,theme:{background:'#111827',foreground:'#d1d5db'}});
    const fit=new FitAddon();terminal.loadAddon(fit);terminal.open(container.current);fit.fit();
    const url=new URL(`/api/v1/sessions/${sessionId}/terminal`,window.location.href);
    url.protocol=url.protocol==='https:'?'wss:':'ws:';
    const ws=new WebSocket(url);socket.current=ws;
    function resize() {fit.fit();if(ws.readyState===WebSocket.OPEN)ws.send(JSON.stringify({type:'resize',cols:Math.min(300,terminal.cols),rows:Math.min(150,terminal.rows)}));}
    ws.onopen=()=>{setConnected(true);onConnection(true);resize();};
    ws.onmessage=event=>{try{const value=JSON.parse(event.data);if(value.type==='output'&&typeof value.data==='string')terminal.write(value.data);}catch{terminal.writeln('\r\n无效终端消息');}};
    const closed=()=>{setConnected(false);onConnection(false);terminal.writeln('\r\n终端已断开。请检查考试状态后重新连接。');};
    ws.onclose=closed;ws.onerror=()=>{ws.close();};
    const input=terminal.onData(data=>{
      // Bound frames even when the user pastes a long command.
      const points=Array.from(data);
      for(let i=0;i<points.length;i+=1024)if(ws.readyState===WebSocket.OPEN)ws.send(JSON.stringify({type:'input',data:points.slice(i,i+1024).join('')}));
    });
    const observer=new ResizeObserver(resize);observer.observe(container.current);
    return()=>{ws.onclose=null;ws.onopen=null;ws.onmessage=null;ws.onerror=null;ws.close();socket.current=null;input.dispose();observer.disconnect();terminal.dispose();onConnection(false);};
  },[sessionId,enabled,attempt,onConnection]);
  useEffect(()=>{
    if(command&&socket.current?.readyState===WebSocket.OPEN)socket.current.send(JSON.stringify({type:'input',data:command.text}));
  },[command]);
  return <div className="terminal-pane"><div className="terminal-head"><span className={`conn ${connected?'online':'offline'}`}>{connected?'终端已连接 · /workspace':'终端未连接'}</span>
    <button disabled={!enabled||connected} onClick={()=>setAttempt(v=>v+1)}>重新连接终端</button></div><div ref={container} className="terminal-body"/></div>;
}
