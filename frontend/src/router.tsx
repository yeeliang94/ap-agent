import { createContext, MouseEvent, ReactNode, useContext, useEffect, useState } from "react";

type LocationState = { pathname: string; search: string };
const RouterContext = createContext<{ location: LocationState; navigate: (to: string, replace?: boolean) => void } | null>(null);

function readLocation(): LocationState {
  return { pathname: window.location.pathname, search: window.location.search };
}

export function BrowserRouter({ children }: { children: ReactNode }) {
  const [location, setLocation] = useState(readLocation);
  useEffect(() => {
    const changed = () => setLocation(readLocation());
    window.addEventListener("popstate", changed);
    return () => window.removeEventListener("popstate", changed);
  }, []);
  const navigate = (to: string, replace = false) => {
    window.history[replace ? "replaceState" : "pushState"]({}, "", to);
    setLocation(readLocation());
    window.scrollTo({ top: 0, behavior: "auto" });
  };
  return <RouterContext.Provider value={{ location, navigate }}>{children}</RouterContext.Provider>;
}

export function useRouter() {
  const value = useContext(RouterContext);
  if (value) return value;
  return { location: readLocation(), navigate: (to: string, replace = false) => {
    window.history[replace ? "replaceState" : "pushState"]({}, "", to);
    window.dispatchEvent(new PopStateEvent("popstate"));
  } };
}

export function Link({ to, children, className, ariaCurrent, onClick }: {
  to: string; children: ReactNode; className?: string; ariaCurrent?: "page"; onClick?: () => void;
}) {
  const { navigate } = useRouter();
  const follow = (event: MouseEvent<HTMLAnchorElement>) => {
    onClick?.();
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigate(to);
  };
  return <a href={to} className={className} aria-current={ariaCurrent} onClick={follow}>{children}</a>;
}

export function setQuery(values: Record<string, string | undefined>): string {
  const query = new URLSearchParams(window.location.search);
  Object.entries(values).forEach(([key, value]) => value ? query.set(key, value) : query.delete(key));
  const suffix = query.toString();
  return window.location.pathname + (suffix ? `?${suffix}` : "");
}
