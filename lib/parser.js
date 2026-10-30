// Pure parser: retain the original command and prompt wording, including :: weights.
export function parseCommand(fullCommand = '') {
  const text = String(fullCommand);
  const starts = [];
  let quote = null;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === '\\') { i++; continue; }
    if (quote) { if (c === quote) quote = null; continue; }
    // Apostrophes in words are not quoting delimiters.
    if ((c === '"' || c === "'") && (i === 0 || /\s/.test(text[i-1]))) { quote = c; continue; }
    if (c === '-' && text[i+1] === '-' && (i === 0 || /\s/.test(text[i-1]))) {
      const match = /^--([a-z][a-z0-9-]*)\b/i.exec(text.slice(i));
      if (match) { starts.push({start:i, end:i+match[0].length, name:match[1].toLowerCase()}); i += match[0].length-1; }
    }
  }
  const flags = {ar:null,v:null,stylize:null,chaos:null,weird:null,sref:[],profile:[],niji:null,raw:false,tile:false,iw:null};
  const aliases = {aspect:'ar',version:'v',s:'stylize',c:'chaos',w:'weird',p:'profile'};
  const unknown = {};
  let profile_default=false;
  const tokens = value => (value.match(/"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\S+/g) || []).map(s => /^(["']).*\1$/.test(s) ? s.slice(1,-1) : s);
  for (let i=0;i<starts.length;i++) {
    const part=starts[i], name=aliases[part.name] || part.name;
    const value=text.slice(part.end,starts[i+1]?.start ?? text.length).trim();
    if (name==='sref' || name==='profile') { flags[name].push(...tokens(value)); if(name==='profile'&&!value)profile_default=true; }
    else if (name==='raw' || name==='tile') flags[name]=true;
    else if (Object.hasOwn(flags,name)) flags[name]=value || true;
    else (unknown[name] ||= []).push(value || true);
  }
  return {prompt:text.slice(0,starts[0]?.start ?? text.length).trim(),full_command:text,flags,profile_default,unknown_flags:unknown};
}
