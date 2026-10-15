import React from 'react';

export type IconName='messages'|'feedback'|'tasks'|'shield'|'handoff'|'attachment'|'box'|'chart'|'upload'|'store'|'copy'|'sparkles'|'arrow-left'|'arrow-right'|'check'|'language'|'user'|'panel'|'warning'|'search';
const paths:Record<IconName,React.ReactNode>={
 search:<><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></>,
 messages:<><path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9H13a8.5 8.5 0 0 1 8 8v.5Z"/><path d="M8 10h8M8 14h5"/></>,
 feedback:<><path d="M20 15a3 3 0 0 1-3 3H9l-5 3V6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3Z"/><path d="M12 7v4m0 3h.01"/></>,
 tasks:<><rect x="5" y="4" width="15" height="17" rx="3"/><path d="M9 4V2m7 2V2M8 10l1 1 2-2m3 1h3M8 16l1 1 2-2m3 1h3"/></>,
 shield:<><path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8 12 3 3 5-6"/></>,
 handoff:<><path d="M3 8h15m-4-4 4 4-4 4M21 16H6m4-4-4 4 4 4"/></>,
 attachment:<path d="m9 17 8-8a3 3 0 0 0-4-4L5 13a5 5 0 0 0 7 7l8-8"/>,
 box:<><path d="m12 3 9 5-9 5-9-5 9-5Zm-9 5v9l9 5 9-5V8M12 13v9M7.5 5.5l9 5"/></>,
 chart:<><path d="M4 3v17h17M8 15v-4m5 4V7m5 8v-6"/></>,
 upload:<><path d="M12 16V3m-5 5 5-5 5 5M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/></>,
 store:<><path d="M4 10v11h16V10M3 10l2-7h14l2 7M3 10c0 3 4 3 4 0 0 3 5 3 5 0 0 3 5 3 5 0 0 3 4 3 4 0M9 21v-6h6v6"/></>,
 copy:<><rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h3"/></>,
 sparkles:<><path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3ZM20 2v4m-2-2h4"/></>,
 'arrow-left':<path d="M20 12H4m6-6-6 6 6 6"/>,
 'arrow-right':<path d="M4 12h16m-6-6 6 6-6 6"/>,
 check:<path d="m5 12 4 4L19 6"/>,
 language:<><path d="M3 5h12M9 3v2m-4 3c2 5 5 8 9 10M13 5c-1 6-4 10-10 13m11 3 4-10 4 10m-6-4h4"/></>,
 user:<><circle cx="12" cy="8" r="4"/><path d="M4 21v-2a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v2"/></>,
 panel:<><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16m-5-12-3 4 3 4"/></>,
 warning:<><path d="m12 3 10 18H2L12 3Z"/><path d="M12 9v5m0 3h.01"/></>,
};
export function Icon({name,className=''}:{name:IconName;className?:string}){
 return <svg className={'ui-icon '+className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">{paths[name]}</svg>;
}
