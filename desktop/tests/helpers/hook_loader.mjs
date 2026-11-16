// Loader for the hook seam test: maps `@novnc/novnc` to the local stub and
// resolves extensionless relative imports out of `.ts` sources (the repo's
// TS style omits extensions; plain `node --test` does not guess them).
export async function resolve(specifier, context, next) {
  if (specifier === "@novnc/novnc") {
    return {
      shortCircuit: true,
      url: new URL("./novnc_rfb_stub.mjs", import.meta.url).href,
    };
  }
  if (
    (specifier.startsWith("./") || specifier.startsWith("../")) &&
    typeof context.parentURL === "string" &&
    /\.tsx?$/.test(context.parentURL) &&
    !/\.[a-zA-Z]+$/.test(specifier)
  ) {
    return {
      shortCircuit: true,
      url: new URL(`${specifier}.ts`, context.parentURL).href,
    };
  }
  return next(specifier, context);
}
