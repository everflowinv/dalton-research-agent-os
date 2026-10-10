/* A transport prefix applies to routes, never to research text or JSON values. */
(function () {
  const base = document.querySelector('meta[name="dalton-base"]')?.content || "";
  if (!/^\/dalton\/w\/[a-z0-9][a-z0-9-]{0,62}\/$/.test(base)) return;
  function url(value) {
    if (typeof value !== "string") return value;
    if (value === "/" || /^\/[#?]/.test(value) || /^\/(v1\/|v2\/|admin(?:[/?#]|$)|legacy(?:[/?#]|$)|favicon\.ico(?:[?#]|$))/.test(value)) return base + value.slice(1);
    return value;
  }
  globalThis.DaltonPublicURL = url;
  const nativeFetch = globalThis.fetch.bind(globalThis);
  globalThis.fetch = (input, options) => {
    if (typeof input === "string") return nativeFetch(url(input), options);
    // URL and Request objects retain their method, headers, body and signal.
    // Only this origin's recognized cockpit paths are transported.
    const isRequest = typeof Request !== "undefined" && input instanceof Request;
    const isURL = typeof URL !== "undefined" && input instanceof URL;
    if ((isRequest || isURL) && globalThis.location) {
      const original = new URL(isRequest ? input.url : input.href);
      if (original.origin === location.origin) {
        const mapped = url(original.pathname + original.search + original.hash);
        if (mapped !== original.pathname + original.search + original.hash) {
          const destination = new URL(mapped, location.origin);
          input = isRequest ? new Request(destination, input) : destination;
        }
      }
    }
    return nativeFetch(input, options);
  };
})();
