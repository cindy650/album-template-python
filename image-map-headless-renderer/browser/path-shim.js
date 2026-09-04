const join = (...parts) => parts.filter(Boolean).join('/').replace(/\/+/g, '/');
export { join };
export default { join };
