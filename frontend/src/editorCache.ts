export type EditorSource={side:'zh'|'en';text:string};
export function recoverEditor(value:unknown,serverDraft:string):EditorSource|null{
 if(!value||typeof value!=='object')return null;
 const cached=value as {baseDraft?:unknown;draft?:unknown;source?:{side?:unknown;text?:unknown}};
 if(cached.baseDraft!==serverDraft&&cached.draft!==serverDraft)return null;
 if(!cached.source||!['zh','en'].includes(String(cached.source.side))||typeof cached.source.text!=='string')return null;
 return {side:cached.source.side as 'zh'|'en',text:cached.source.text};
}
