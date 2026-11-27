const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('static/navigation.js','utf8');
function setup(storage=new Map(),wide=true,blocked=false){
  const events={},attrs={},classes=new Set(),media={matches:wide,addEventListener(type,fn){this.change=fn;}};
  const button={setAttribute(k,v){attrs[k]=v;},addEventListener(k,fn){events[k]=fn;}};
  const context=vm.createContext({document:{querySelector:()=>({after(){}}),createElement:()=>button,addEventListener(){},body:{classList:{toggle(k,on){on?classes.add(k):classes.delete(k);}}}},
    matchMedia:()=>media,localStorage:{getItem(k){if(blocked)throw Error('blocked');return storage.get(k);},setItem(k,v){if(blocked)throw Error('blocked');storage.set(k,v);}}});
  vm.runInContext(source,context);
  return {attrs,classes,media,click:()=>events.click()};
}
const storage=new Map(),first=setup(storage);
assert.equal(first.attrs['aria-expanded'],'true','sidebar starts expanded');
first.click();assert.equal(first.attrs['aria-expanded'],'false');
assert(first.classes.has('sidebar-collapsed'));
const reloaded=setup(storage);assert.equal(reloaded.attrs['aria-expanded'],'false','refresh remembers collapse');
reloaded.media.matches=false;reloaded.media.change();assert.equal(reloaded.attrs['aria-expanded'],'true','mobile navigation remains visible');
assert(!reloaded.classes.has('sidebar-collapsed'));
reloaded.media.matches=true;reloaded.media.change();assert(reloaded.classes.has('sidebar-collapsed'));
reloaded.click();assert.equal(reloaded.attrs['aria-expanded'],'true');
const restricted=setup(new Map(),true,true);restricted.click();assert.equal(restricted.attrs['aria-expanded'],'false');
console.log('PASS: sidebar toggles, persists, stays usable on mobile and with blocked storage');
