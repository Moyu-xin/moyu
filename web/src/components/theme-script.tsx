const themeScript = `
(() => {
  try {
    document.documentElement.classList.remove("dark");
    document.documentElement.style.colorScheme = "light";
  } catch {
    document.documentElement.classList.remove("dark");
    document.documentElement.style.colorScheme = "light";
  }
})();
`;

export function ThemeScript() {
  return <script dangerouslySetInnerHTML={{ __html: themeScript }} />;
}
