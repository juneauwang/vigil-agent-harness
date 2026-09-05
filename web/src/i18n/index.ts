/**
 * i18n 初始化（react-i18next + i18next）：默认 zh（现有用户预期），语言选择
 * 持久化到 localStorage。任意入口 import "@/i18n" 即完成初始化（幂等），
 * 组件内用 useTranslation() 取 t；模块级非组件代码用默认导出的 i18n 实例
 * 调 i18n.t()。
 */
import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import zh from "./zh";
import en from "./en";

export type Lang = "zh" | "en";
export const LANG_STORAGE_KEY = "vigil-console-lang";

function initialLang(): Lang {
  try {
    const saved = globalThis.localStorage?.getItem(LANG_STORAGE_KEY);
    if (saved === "en" || saved === "zh") return saved;
  } catch { /* ignore: node env / storage disabled */ }
  return "zh";
}

void i18n.use(initReactI18next).init({
  resources: {
    zh: { translation: zh },
    en: { translation: en },
  },
  lng: initialLang(),
  fallbackLng: "zh",
  interpolation: { escapeValue: false },
});

export function currentLang(): Lang {
  return i18n.language === "en" ? "en" : "zh";
}

/** 切换语言并持久化（useTranslation 订阅的组件自动重渲染）。 */
export function switchLang(lang: Lang): void {
  void i18n.changeLanguage(lang);
  try {
    globalThis.localStorage?.setItem(LANG_STORAGE_KEY, lang);
  } catch { /* ignore */ }
}

export default i18n;
