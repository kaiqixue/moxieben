const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

async function mount(data, error) {
  const calls = [];
  const context = vm.createContext({
    document: {addEventListener() {}}, clearTimeout() {},
    api: async path => {
      calls.push(path);
      if (error) throw Object.assign(new Error('forbidden'), {status: error});
      return {questions: [], students: []};
    }
  });
  vm.runInContext(fs.readFileSync('static/translation.js', 'utf8'), context);
  context.assignmentData = data;
  await vm.runInContext('Translation.mountAssignment(assignmentData)', context);
  return calls;
}

(async () => {
  assert.deepEqual(await mount({id: 10, has_translation: false}, 403), [],
    'ordinary assignments must not request an unavailable translation endpoint');
  assert.equal((await mount({id: 10, questions: [{scoring_mode: 'strict'}]})).length, 0);
  assert.equal((await mount({id: 10, has_translation: true})).length, 1);
  assert.equal((await mount({id: 10})).length, 1, 'keep compatibility with older assignment responses');
  await assert.rejects(mount({id: 10, has_translation: true}, 403), {status: 403},
    'real permission failures must remain visible');
  console.log('PASS: ordinary grades avoid translation requests; translation permission errors stay visible');
})().catch(error => {console.error(error); process.exitCode = 1;});
