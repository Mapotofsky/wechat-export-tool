import React, {useLayoutEffect,useRef,useState} from 'react';
import {createPortal} from 'react-dom';

export type ChatMenuTarget={username:string;x:number;y:number;opener:HTMLElement};
type Action={label:string;disabled?:boolean;danger?:boolean;run:()=>void};

export default function ChatContextMenu({target,name,actions,onClose}:{target:ChatMenuTarget;name:string;actions:Action[];onClose:()=>void}){
 const menu=useRef<HTMLDivElement>(null),[position,setPosition]=useState({left:target.x,top:target.y});
 function dismiss(restore=false){onClose();if(restore&&target.opener.isConnected)target.opener.focus()}
 useLayoutEffect(()=>{
  const el=menu.current!;
  const list=target.opener.closest('.session-scroll');
  const initialScroll=list?.scrollTop;
  const place=()=>{const box=el.getBoundingClientRect();setPosition({left:Math.max(8,Math.min(target.x,window.innerWidth-box.width-8)),top:Math.max(8,Math.min(target.y,window.innerHeight-box.height-8))})};
  place();el.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus({preventScroll:true});
  const outside=(e:PointerEvent)=>{if(!el.contains(e.target as Node))dismiss()};
  // A browser may deliver the scroll that revealed the row after contextmenu.
  const scroll=(e:Event)=>{if(!el.contains(e.target as Node)&&(e.target!==list||list?.scrollTop!==initialScroll))dismiss()};
  const resize=()=>dismiss();
  document.addEventListener('pointerdown',outside);document.addEventListener('scroll',scroll,true);window.addEventListener('resize',resize);
  return()=>{document.removeEventListener('pointerdown',outside);document.removeEventListener('scroll',scroll,true);window.removeEventListener('resize',resize)};
 },[target]);
 function keydown(e:React.KeyboardEvent){
  const items=Array.from(menu.current!.querySelectorAll<HTMLButtonElement>('button:not(:disabled)'));
  const i=items.indexOf(document.activeElement as HTMLButtonElement);
  if(e.key==='Escape'){e.preventDefault();e.stopPropagation();dismiss(true)}
  else if(e.key==='Tab')dismiss();
  else if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)){e.preventDefault();const next=e.key==='Home'?0:e.key==='End'?items.length-1:(i+(e.key==='ArrowUp'?-1:1)+items.length)%items.length;items[next]?.focus()}
 }
 return createPortal(<div className="chat-context-menu" ref={menu} role="menu" aria-label={'管理会话：'+name} style={position} onKeyDown={keydown} onContextMenu={e=>e.preventDefault()}>
  <div className="chat-menu-heading" role="presentation"><strong>{name}</strong><small>{target.username}</small></div>
  {actions.map(a=><button key={a.label} role="menuitem" disabled={a.disabled} className={a.danger?'danger-action':''} onClick={()=>{dismiss(true);a.run()}}>{a.label}</button>)}
 </div>,document.body);
}
