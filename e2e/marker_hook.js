const path = require("path");
const marker = process.env.ASG_MARKER_FILE || path.join(__dirname, "marker.txt");
require("fs").appendFileSync(marker, "HOOK_LOADED pid=" + process.pid + " exec=" + process.execPath + "\n");
