export function credential(role:string){return sessionStorage.getItem('seller-token')||'';}
export function actorId(role:string){return sessionStorage.getItem('seller-actor')||`local-${role}`;}
export function clearSession(){for(const k of ['seller-token','seller-actor','seller-role','seller-shop'])sessionStorage.removeItem(k);}
export function currentRole(){
 const role=sessionStorage.getItem('seller-role');
 if(role==='restricted'||sessionStorage.getItem('seller-actor')==='local-restricted')clearSession();
 return role==='supervisor'?'supervisor':'agent';
}
