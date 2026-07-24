const { isConfigured, json, supabaseFetch } = require("./_supabase");

module.exports = async (req, res) => {
  if (req.method !== "GET") {
    return json(res, 405, { error: "Method not allowed" });
  }
  if (!isConfigured()) {
    return json(res, 200, { schedules: {} });
  }

  const requestUrl = new URL(req.url, "https://bandi-shuttle-viewer.vercel.app");
  const monthKey = (requestUrl.searchParams.get("month_key") || "").trim();
  const filters = ["select=date_key,schedule_json", "order=date_key.asc"];
  if (/^\d{4}-\d{2}$/.test(monthKey)) {
    filters.push(`month_key=eq.${encodeURIComponent(monthKey)}`);
  }

  const response = await supabaseFetch(
    `/rest/v1/schedule_days?${filters.join("&")}`,
    { method: "GET" }
  );
  if (!response.ok) {
    return json(res, response.status, { error: "Failed to load schedule days" });
  }

  const rows = await response.json();
  const schedules = {};
  for (const row of rows) {
    if (row && row.date_key && row.schedule_json) {
      schedules[row.date_key] = row.schedule_json;
    }
  }
  return json(res, 200, { schedules });
};
