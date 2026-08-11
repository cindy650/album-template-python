const SPREADSHEET_ID = "1Rat48CbyRpfb7QCdEufidJ5XuAfdTLehiHPUMyjtgPc";
const SHEET_GID = 0;
const WEBHOOK_TOKEN = "69eb3cdafc454e98b8e10b42b47f07cb";

const HEADERS = [
  "订单号",
  "店铺",
  "店铺名",
  "产品",
  "商品信息",
  "付款方式",
  "邮寄地址",
  "交易编号",
  "数量",
];

function doGet() {
  return jsonResponse_({
    ok: true,
    service: "Etsy order webhook",
  });
}

function doPost(event) {
  try {
    if (!event || !event.postData || !event.postData.contents) {
      throw new Error("请求正文为空");
    }

    const order = JSON.parse(event.postData.contents);
    if (order.token !== WEBHOOK_TOKEN) {
      throw new Error("Webhook token 不正确");
    }

    const sheet = getTargetSheet_();
    validateHeaders_(sheet);

    const orderNumber = textValue_(order["订单号"]);
    const transactionId = textValue_(order["交易编号"]);
    const duplicateRow = findDuplicateRow_(
      sheet,
      orderNumber,
      transactionId,
    );
    if (duplicateRow) {
      return jsonResponse_({
        ok: true,
        duplicate: true,
        row: duplicateRow,
        orderNumber: orderNumber,
      });
    }

    const row = [
      safeCellText_(orderNumber),
      safeCellText_(order["店铺"]),
      safeCellText_(order["店铺名"]),
      safeCellText_(order["产品"]),
      safeCellText_(formatProductInformation_(order["商品信息"])),
      safeCellText_(order["付款方式"]),
      safeCellText_(order["邮寄地址"]),
      safeCellText_(transactionId),
      safeCellText_(order["数量"]),
    ];

    const lock = LockService.getScriptLock();
    lock.waitLock(30000);
    let rowNumber;
    try {
      const duplicateAfterLock = findDuplicateRow_(
        sheet,
        orderNumber,
        transactionId,
      );
      if (duplicateAfterLock) {
        return jsonResponse_({
          ok: true,
          duplicate: true,
          row: duplicateAfterLock,
          orderNumber: orderNumber,
        });
      }

      rowNumber = sheet.getLastRow() + 1;
      const range = sheet.getRange(rowNumber, 1, 1, HEADERS.length);
      range.setValues([row]);
      range.setVerticalAlignment("top");
      range.setWrap(true);
      SpreadsheetApp.flush();
    } finally {
      lock.releaseLock();
    }

    return jsonResponse_({
      ok: true,
      duplicate: false,
      row: rowNumber,
      orderNumber: orderNumber,
    });
  } catch (error) {
    return jsonResponse_({
      ok: false,
      error: String(error && error.message ? error.message : error),
    });
  }
}

function getTargetSheet_() {
  const spreadsheet = SpreadsheetApp.openById(SPREADSHEET_ID);
  const sheet = spreadsheet
    .getSheets()
    .find((candidate) => candidate.getSheetId() === SHEET_GID);
  if (!sheet) {
    throw new Error("找不到 gid=" + SHEET_GID + " 的工作表");
  }
  return sheet;
}

function validateHeaders_(sheet) {
  const actual = sheet
    .getRange(1, 1, 1, HEADERS.length)
    .getDisplayValues()[0];
  const mismatches = HEADERS.filter(
    (expected, index) => actual[index].trim() !== expected,
  );
  if (mismatches.length) {
    throw new Error(
      "表头与脚本不一致，缺少或位置错误：" + mismatches.join(", "),
    );
  }
}

function findDuplicateRow_(sheet, orderNumber, transactionId) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2 || (!orderNumber && !transactionId)) {
    return 0;
  }

  const orderNumberIndex = HEADERS.indexOf("订单号");
  const transactionIdIndex = HEADERS.indexOf("交易编号");
  const rows = sheet
    .getRange(2, 1, lastRow - 1, HEADERS.length)
    .getDisplayValues();
  const index = rows.findIndex((row) => {
    if (orderNumber && row[orderNumberIndex].trim() !== orderNumber) {
      return false;
    }
    if (transactionId && row[transactionIdIndex].trim() !== transactionId) {
      return false;
    }
    return true;
  });
  return index < 0 ? 0 : index + 2;
}

function safeCellText_(value) {
  const text = textValue_(value);
  return /^[=+\-@]/.test(text) ? "'" + text : text;
}

function formatProductInformation_(value) {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "object") {
    return Object.entries(value)
      .map(([field, fieldValue]) => field + ": " + textValue_(fieldValue))
      .join("\n");
  }
  return String(value).replace(/\r\n?/g, "\n").trim();
}

function textValue_(value) {
  return value === null || value === undefined ? "" : String(value).trim();
}

function jsonResponse_(payload) {
  return ContentService
    .createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}
