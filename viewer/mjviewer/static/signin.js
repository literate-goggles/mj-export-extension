// Hosted mode, no session: start the sign-in from this page so that it comes back to
// this exact view. The part after '#' never reaches the server, so a redirect from
// the server would lose it; Google's account chooser would too.
const next = `${location.pathname}${location.search}${location.hash}` || '/';
location.replace(`/auth/login?next=${encodeURIComponent(next)}`);
