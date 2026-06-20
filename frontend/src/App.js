import { useEffect } from "react";
import "@/App.css";

function App() {
  useEffect(() => {
    window.location.replace("/hub/index.html");
  }, []);
  return (
    <div style={{
      background: "#020814", color: "#9AA3C0", height: "100vh",
      display: "flex", alignItems: "center", justifyContent: "center",
      fontFamily: "'JetBrains Mono', monospace", letterSpacing: "1.5px",
      textTransform: "uppercase", fontSize: "12px"
    }}>
      Entering YABBAI.NETWORK…
    </div>
  );
}

export default App;
