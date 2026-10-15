import test from 'node:test';
import assert from 'node:assert/strict';
import {recoverEditor} from '../src/editorCache.ts';

test('unsaved Chinese survives re-entry while translation has not finished',()=>{
 const source={side:'zh',text:'请核对设备型号。'};
 assert.deepEqual(recoverEditor({source,baseDraft:'Old English',draft:''},'Old English'),source);
});
test('edited English and translated Chinese drafts survive a sidebar page change',()=>{
 const source={side:'en',text:'Please provide the model.'};
 assert.deepEqual(recoverEditor({source,baseDraft:'Old English',draft:source.text},'Old English'),source);
 assert.deepEqual(recoverEditor({source,baseDraft:'Old English',draft:source.text},source.text),source);
});
test('server-side changes invalidate unsaved cache instead of overwriting newer work',()=>{
 assert.equal(recoverEditor({source:{side:'en',text:'My local edit'},baseDraft:'Old',draft:'My local edit'},'New server draft'),null);
});
test('legacy matching drafts remain recoverable and corrupted cache is ignored',()=>{
 const source={side:'zh',text:'请核对。'};
 assert.deepEqual(recoverEditor({source,draft:'Saved English'},'Saved English'),source);
 assert.equal(recoverEditor({source,draft:'Different'},'Saved English'),null);
 for(const value of [null,[],{},'text',{draft:'Saved English',source:{side:'other',text:'x'}},{draft:'Saved English',source:{side:'en',text:7}}])assert.equal(recoverEditor(value,'Saved English'),null);
});
