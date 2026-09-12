(function installImageMapHeadlessRenderer(global) {
  'use strict';

  const fabric = global.fabric;
  if (!fabric) throw new Error('Fabric 7.4.0 未加载');

  const CANVAS_ROW_GAP = 0;
  const PIXELS_PER_UNIT = Object.freeze({
    in: 96,
    cm: 96 / 2.54,
    mm: 96 / 25.4,
  });

  installTextWordSpacingSupport();

  global.ImageMapHeadlessRenderer = Object.freeze({
    render,
    version: '0.1.0',
    fabricVersion: fabric.version,
  });

  async function render(options) {
    const source = clone(options?.json);
    const rawObjects = extractObjects(source);
    const workarea = rawObjects.find(item => String(item?.id || '').toLowerCase() === 'workarea');
    if (!workarea) throw new Error('图层 JSON 缺少 id=workarea 的画布对象');

    normalizeWorkareaPrintGeometry(workarea);

    const bounds = resolveWorkareaBounds(workarea);
    syncSpineEndBlocks(rawObjects, workarea, bounds);
    // Product safe distances are supplied by the backend adapter.  Keep the
    // renderer compatible with the frontend's camelCase/snake_case payloads,
    // while preserving the backend-only product switch and defaults.
    const renderWorkarea = {
      ...workarea,
      ...resolveSafeDistances(options, source, workarea),
    };
    applyAlignmentMarkers(rawObjects, workarea);
    const cropWidth = Math.ceil(bounds.width);
    const cropHeight = Math.ceil(bounds.height);
    const dpi = positive(options?.dpi ?? 300, 'dpi');
    const quality = bounded(options?.quality ?? 0.95, 0, 1, 'quality');
    const formats = normalizeFormats(options?.formats);
    const multiplier = dpi / 96;
    const canvasElement = global.document.getElementById('image-map-render-canvas')
      || global.document.createElement('canvas');
    const canvas = new fabric.StaticCanvas(canvasElement, {
      width: cropWidth,
      height: cropHeight,
      renderOnAddRemove: false,
      enableRetinaScaling: false,
      backgroundColor: options?.backgroundColor || workarea.backgroundColor || '#ffffff',
    });
    canvas.setDimensions({ width: cropWidth, height: cropHeight });

    const warnings = [];
    let textOverflowBlocked = false;
    await loadFonts(rawObjects, warnings);
    const objectsToRender = [];
    if (typeof workarea.src === 'string' && workarea.src) objectsToRender.push(workarea);
    objectsToRender.push(...rawObjects.filter(item => String(item?.id || '').toLowerCase() !== 'workarea'));

    for (const serialized of objectsToRender) {
      const local = toWorkareaScene(serialized, bounds);
      const object = await createObject(local, warnings);
      if (!object) continue;
      const fitResult = fitTextObjectToSafeRegion(
        object,
        serialized,
        renderWorkarea,
        bounds,
        options,
      );
      warnings.push(...fitResult.warnings);
      textOverflowBlocked = textOverflowBlocked || fitResult.blocked;
      canvas.add(object);
      object.setCoords?.();
    }
    if (textOverflowBlocked) {
      canvas.dispose();
      throw new Error(warnings.filter(item => item.includes('已阻止导出')).join('\n') || '存在文字图层超出安全区域，已阻止导出');
    }

    canvas.backgroundColor = options?.backgroundColor || workarea.backgroundColor || '#ffffff';
    canvas.renderAll();
    const outputCanvas = canvas.toCanvasElement(multiplier, {
      left: 0,
      top: 0,
      width: cropWidth,
      height: cropHeight,
    });
    const images = {};
    if (formats.includes('jpg')) {
      images.jpg = outputCanvas.toDataURL('image/jpeg', quality);
    }
    if (formats.includes('png')) {
      images.png = outputCanvas.toDataURL('image/png');
    }
    if (formats.includes('svg')) {
      images.svg = buildSvg(canvas, workarea, bounds, rawObjects);
    }
    if (formats.includes('text-to-svg')) {
      images.textToSvg = await buildTextToSvg(canvas, workarea, bounds, rawObjects);
    }
    const geometry = canvas.getObjects().map(object => ({
      id: object.id ?? null,
      type: object.type,
      left: object.left,
      top: object.top,
      width: object.width,
      height: object.height,
      scaleX: object.scaleX,
      scaleY: object.scaleY,
    }));
    const result = {
      images,
      // Return the cloned document after text fitting and alignment so callers
      // can persist the exact fontSize values used to generate the files.
      // The object passed in options.json is never mutated.
      json: source,
      width: outputCanvas.width,
      height: outputCanvas.height,
      cssWidth: cropWidth,
      cssHeight: cropHeight,
      dpi,
      objectCount: canvas.getObjects().length,
      geometry,
      warnings,
      fabricVersion: fabric.version,
    };
    canvas.dispose();
    return result;
  }

  function extractObjects(value) {
    if (Array.isArray(value)) return value;
    if (Array.isArray(value?.objects)) return value.objects;
    if (Array.isArray(value?.layers?.objects)) return value.layers.objects;
    throw new Error('图层 JSON 必须是对象数组，或包含 objects/layers.objects 数组');
  }

  /**
   * Keep the standalone renderer aligned with WorkareaHandler's two-row
   * geometry. Older/simplified payloads may carry canvasRows=2 while their
   * Fabric width/height or printGuides still describe a single row.
   */
  function normalizeWorkareaPrintGeometry(workarea) {
    if (workarea.canvasRows !== 2) return;

    const factor = PIXELS_PER_UNIT[String(workarea.unit || '').toLowerCase()];
    const sideHeight = nonnegativeOrNull(workarea.sideHeight);
    const bleed = nonnegativeOrNull(workarea.bleed);
    const separateBleed = workarea.separateBleed === true;
    const horizontalBleed = separateBleed ? nonnegativeOrNull(workarea.horizontalBleed) : bleed;
    const verticalBleed = separateBleed ? nonnegativeOrNull(workarea.verticalBleed) : bleed;
    if (!factor || sideHeight === null || horizontalBleed === null || verticalBleed === null) return;

    const rowHeight = Math.max(1, (sideHeight + verticalBleed * 2) * factor);
    const rowGap = nonnegativeOrNull(workarea.canvasRowGap) ?? CANVAS_ROW_GAP;
    const height = rowHeight * 2 + rowGap;
    const sideWidth = nonnegativeOrNull(workarea.sideWidth);
    const width = sideWidth !== null
      ? Math.max(1, (sideWidth * 2 + horizontalBleed * 2) * factor)
      : positiveOrNull(workarea.workareaWidth) || positiveOrNull(workarea.width);

    if (width) {
      workarea.width = width;
      workarea.workareaWidth = width;
    }
    // Two-row layouts intentionally have no spine. Normalize these fields so
    // the returned JSON and all downstream exporters agree with the geometry.
    workarea.spineWidth = 0;
    workarea.spineBleed = 0;
    workarea.height = height;
    workarea.workareaHeight = height;
    workarea.printGuides = buildPrintGuides(width, height, {
      factor,
      sideWidth,
      horizontalBleed,
      verticalBleed,
      spineWidth: 0,
      spineBleed: 0,
      canvasRows: 2,
      canvasRowGap: rowGap,
    }, workarea.printGuides);
  }

  function syncSpineEndBlocks(objects, workarea, bounds) {
    const ids = new Set(['__spine-top-block', '__spine-bottom-block']);
    for (let index = objects.length - 1; index >= 0; index -= 1) {
      if (ids.has(String(objects[index]?.id || ''))) objects.splice(index, 1);
    }
    const blocks = buildSpineEndBlocks(workarea, bounds);
    if (!blocks.length) return blocks;
    const workareaIndex = objects.indexOf(workarea);
    objects.splice(workareaIndex >= 0 ? workareaIndex + 1 : 0, 0, ...blocks);
    return blocks;
  }

  function buildSpineEndBlocks(workarea, bounds) {
    if (workarea.canvasRows === 2) return [];
    const unit = String(workarea.unit || '').toLowerCase();
    const factor = PIXELS_PER_UNIT[unit];
    const spineWidth = Math.max(0, numberOr(workarea.spineWidth, 0));
    if (!factor || !(spineWidth > 0)) return [];

    const bleed = Math.max(0, numberOr(workarea.bleed, 0));
    const horizontalBleed = Math.max(0, numberOr(
      workarea.separateBleed === true ? workarea.horizontalBleed : bleed,
      bleed,
    ));
    const sideWidth = Math.max(0, numberOr(workarea.sideWidth, 0));
    const spineBleed = Math.max(0, numberOr(workarea.spineBleed, 0));
    const logicalWidth = positiveOrNull(workarea.workareaWidth)
      || positiveOrNull(workarea.width)
      || bounds.width;
    const logicalHeight = positiveOrNull(workarea.workareaHeight)
      || positiveOrNull(workarea.height)
      || bounds.height;
    const scaleX = bounds.width / logicalWidth;
    const scaleY = bounds.height / logicalHeight;
    const left = bounds.left
      + (horizontalBleed + sideWidth + spineBleed) * factor * scaleX;
    const width = spineWidth * factor * scaleX;
    const height = Math.min(bounds.height, 5 * PIXELS_PER_UNIT.mm * scaleY);
    const makeBlock = (id, name, top) => ({
      id,
      name,
      type: 'Rect',
      originX: 'left',
      originY: 'top',
      left,
      top,
      width,
      height,
      scaleX: 1,
      scaleY: 1,
      angle: 0,
      fill: '#000000',
      stroke: null,
      strokeWidth: 0,
      selectable: false,
      evented: false,
    });
    return [
      makeBlock('__spine-top-block', '背脊顶部黑块', bounds.top),
      makeBlock('__spine-bottom-block', '背脊底部黑块', bounds.top + bounds.height - height),
    ];
  }

  /**
   * Reapply the editor's persistent region-centering markers before Fabric
   * creates any objects. The input document has already been cloned, so these
   * corrections affect every export format without mutating the caller's JSON.
   */
  function applyAlignmentMarkers(objects, workarea) {
    const bounds = resolveWorkareaBounds(workarea);
    const unit = String(workarea.unit || '').toLowerCase();
    const factor = PIXELS_PER_UNIT[unit] || PIXELS_PER_UNIT.in;
    const logicalWidth = positiveOrNull(workarea.workareaWidth) || positiveOrNull(workarea.width) || bounds.width;
    const logicalHeight = positiveOrNull(workarea.workareaHeight) || positiveOrNull(workarea.height) || bounds.height;
    const scaleX = bounds.width / logicalWidth;
    const scaleY = bounds.height / logicalHeight;
    const bleed = Math.max(0, numberOr(workarea.bleed, 0));
    const horizontalBleed = Math.max(0, numberOr(
      workarea.separateBleed === true ? workarea.horizontalBleed : bleed,
      bleed,
    )) * factor;
    const verticalBleed = Math.max(0, numberOr(
      workarea.separateBleed === true ? workarea.verticalBleed : bleed,
      bleed,
    )) * factor;
    const sideWidth = Math.max(0, numberOr(workarea.sideWidth, 0)) * factor;
    const rows = workarea.canvasRows === 2 ? 2 : 1;
    const spineBleed = rows === 2 ? 0 : Math.max(0, numberOr(workarea.spineBleed, 0)) * factor;
    const spineWidth = rows === 2 ? 0 : Math.max(0, numberOr(workarea.spineWidth, 0)) * factor;
    const horizontalBounds = rows === 2
      ? [
        horizontalBleed * scaleX,
        (horizontalBleed + sideWidth) * scaleX,
        (logicalWidth - horizontalBleed - sideWidth) * scaleX,
        (logicalWidth - horizontalBleed) * scaleX,
      ]
      : [
        horizontalBleed * scaleX,
        (horizontalBleed + sideWidth + spineBleed) * scaleX,
        (horizontalBleed + sideWidth + spineBleed + spineWidth) * scaleX,
        (logicalWidth - horizontalBleed) * scaleX,
      ];
    const rowGap = rows === 2 ? Math.max(0, numberOr(workarea.canvasRowGap, CANVAS_ROW_GAP)) : 0;
    const rowHeight = Math.max(1, (logicalHeight - rowGap * (rows - 1)) / rows);
    const verticalBounds = [];
    for (let row = 0; row < rows; row += 1) {
      const rowTop = row * (rowHeight + rowGap);
      verticalBounds.push(
        (rowTop + verticalBleed) * scaleY,
        (rowTop + rowHeight - verticalBleed) * scaleY,
      );
    }

    for (const object of objects) {
      if (!object || String(object.id || '').toLowerCase() === 'workarea') continue;
      if (object.horizontalCentered !== true && object.verticalCentered !== true) continue;

      const center = getSerializedObjectCenter(object);
      if (object.horizontalCentered === true) {
        const current = center.x - bounds.left;
        const [start, end] = findHorizontalRegion(horizontalBounds, current, bounds.width);
        const target = bounds.left + (start + end) / 2;
        object.left = numberOr(object.left, 0) + target - center.x;
        center.x = target;
      }
      if (object.verticalCentered === true) {
        const current = center.y - bounds.top;
        const [start, end] = findVerticalRegion(verticalBounds, current, bounds.height);
        const target = bounds.top + (start + end) / 2;
        object.top = numberOr(object.top, 0) + target - center.y;
        center.y = target;
      }
    }
  }

  function findHorizontalRegion(boundaries, current, size) {
    for (let index = 0; index < boundaries.length - 1; index += 1) {
      if (current >= boundaries[index] && current <= boundaries[index + 1]) {
        return [boundaries[index], boundaries[index + 1]];
      }
    }
    if (current < boundaries[0]) return [boundaries[0], boundaries[1] ?? size];
    return [boundaries.at(-2) ?? 0, boundaries.at(-1) ?? size];
  }

  function findVerticalRegion(boundaries, current, size) {
    for (let index = 0; index + 1 < boundaries.length; index += 2) {
      if (current >= boundaries[index] && current <= boundaries[index + 1]) {
        return [boundaries[index], boundaries[index + 1]];
      }
    }

    let nearestIndex = 0;
    let nearestDistance = Number.POSITIVE_INFINITY;
    for (let index = 0; index + 1 < boundaries.length; index += 2) {
      const distance = current < boundaries[index]
        ? boundaries[index] - current
        : current - boundaries[index + 1];
      if (distance < nearestDistance) {
        nearestDistance = distance;
        nearestIndex = index;
      }
    }
    return [boundaries[nearestIndex] ?? 0, boundaries[nearestIndex + 1] ?? size];
  }

  // Fabric's measured dimensions are authoritative after a font-size change.
  // Centering from serialized width/height can use stale editor dimensions and
  // leave a fitted text layer visibly off-center.
  function centerFabricObjectInPrintRegion(object, serialized, regions) {
    if (!object || !serialized || typeof object.getCenterPoint !== 'function') return;
    const current = object.getCenterPoint();
    let nextX = current.x;
    let nextY = current.y;
    if (serialized.horizontalCentered === true) {
      const face = nearestRegion(current.x, regions.faces);
      nextX = (face.start + face.end) / 2;
    }
    if (serialized.verticalCentered === true) {
      const row = nearestRegion(current.y, regions.rows);
      nextY = (row.start + row.end) / 2;
    }
    if (nextX === current.x && nextY === current.y) return;
    if (typeof object.setPositionByOrigin === 'function') {
      object.setPositionByOrigin(new fabric.Point(nextX, nextY), 'center', 'center');
    } else {
      object.left = numberOr(object.left, 0) + nextX - current.x;
      object.top = numberOr(object.top, 0) + nextY - current.y;
    }
    object.setCoords?.();
  }

  function getSerializedObjectCenter(object) {
    const rawWidth = finiteOrNull(object.width ?? object.workareaWidth) ?? 0;
    const rawHeight = finiteOrNull(object.height ?? object.workareaHeight) ?? 0;
    const width = Math.abs(rawWidth * numberOr(object.scaleX, 1));
    const height = Math.abs(rawHeight * numberOr(object.scaleY, 1));
    const originX = String(object.originX || 'left').toLowerCase();
    const originY = String(object.originY || 'top').toLowerCase();
    const offsetX = originX === 'center' ? 0 : originX === 'right' ? -width / 2 : width / 2;
    const offsetY = originY === 'center' ? 0 : originY === 'bottom' ? -height / 2 : height / 2;
    const angle = numberOr(object.angle, 0) * Math.PI / 180;
    const rotatedX = offsetX * Math.cos(angle) - offsetY * Math.sin(angle);
    const rotatedY = offsetX * Math.sin(angle) + offsetY * Math.cos(angle);
    return {
      x: numberOr(object.left, 0) + rotatedX,
      y: numberOr(object.top, 0) + rotatedY,
    };
  }

  function fitTextObjectToSafeRegion(object, serialized, workarea, bounds, options) {
    if (!object || String(serialized?.id || '').toLowerCase() === 'workarea') return { warnings: [], blocked: false };
    // Product configuration controls whether text safety monitoring is
    // enabled for this render. An explicit false must bypass both the
    // margin calculation and automatic font-size reduction; otherwise the
    // renderer's compatibility defaults would still apply.
    if (options?.useSafeDistance === false) return { warnings: [], blocked: false };
    if (typeof object.getObjects === 'function' && object.getObjects().length) {
      const children = object.getObjects();
      const serializedChildren = Array.isArray(serialized?.objects) ? serialized.objects : [];
      return children.reduce((result, child, index) => {
        const childResult = fitTextObjectToSafeRegion(
          child,
          serializedChildren.find(item => item?.id != null && item.id === child.id) || serializedChildren[index] || {},
          workarea,
          bounds,
          options,
        );
        result.warnings.push(...childResult.warnings);
        result.blocked = result.blocked || childResult.blocked;
        return result;
      }, { warnings: [], blocked: false });
    }
    const type = String(serialized?.type || object.type || '').toLowerCase();
    if (!['text', 'i-text', 'textbox'].includes(type) && serialized?.superType !== 'text') return { warnings: [], blocked: false };
    const originalSize = numberOr(object.fontSize, 0);
    if (!(originalSize > 0) || typeof object.getCenterPoint !== 'function' || typeof object.getBoundingRect !== 'function') return { warnings: [], blocked: false };

    const regions = buildTextSafeRegions(workarea, bounds, options);
    if (!regions) return { warnings: [], blocked: false };
    // A marked layer is centered before the first safety measurement. An
    // unmarked layer remains at its authored position.
    centerFabricObjectInPrintRegion(object, serialized, regions);
    const center = object.getCenterPoint();
    // `object` has already been translated to workarea-local coordinates by
    // toWorkareaScene. Subtracting the workarea origin again misclassifies
    // layers whenever the authored workarea has a non-zero left/top.
    const face = nearestRegion(center.x, regions.faces);
    const row = nearestRegion(center.y, regions.rows);
    const margins = face.name === '背脊'
      ? regions.spineMargin
      : face.name === '封底'
        ? regions.backCoverMargin
        : regions.coverMargin;
    const [faceSafeStart, faceSafeEnd] = insetRegion(
      face.start,
      face.end,
      margins.left * regions.scaleX,
      margins.right * regions.scaleX,
    );
    const [rowSafeStart, rowSafeEnd] = insetRegion(
      row.start,
      row.end,
      margins.top * regions.scaleY,
      margins.bottom * regions.scaleY,
    );
    // `object` is already converted by toWorkareaScene, therefore both the
    // object bounds and the safe regions are in workarea-local coordinates.
    const safeLeft = faceSafeStart;
    const safeRight = faceSafeEnd;
    const safeTop = rowSafeStart;
    const safeBottom = rowSafeEnd;
    let rect = object.getBoundingRect();
    const isOverflowing = value => Boolean(value && (
      value.left < safeLeft - 0.01 ||
      value.left + value.width > safeRight + 0.01 ||
      value.top < safeTop - 0.01 ||
      value.top + value.height > safeBottom + 0.01
    ));
    // Keep measuring after every 0.5-pixel font-size change. A proportional
    // adjustment can make a text object unnecessarily small when font metrics,
    // kerning, or explicit line height differ from the initial estimate.
    while (isOverflowing(rect)) {
      const current = numberOr(object.fontSize, 1);
      if (current <= 1) break;
      const next = Math.max(1, current - 0.5);
      if (!(next < current)) break;
      object.set('fontSize', next);
      object.initDimensions?.();
      object.setCoords?.();
      rect = object.getBoundingRect();
    }
    // Font fitting changes Fabric's measured width/height. Run the marker
    // centering one final time against that actual geometry before exporting.
    centerFabricObjectInPrintRegion(object, serialized, regions);
    if (serialized) {
      serialized.left = numberOr(object.left, 0) + bounds.left;
      serialized.top = numberOr(object.top, 0) + bounds.top;
    }
    rect = object.getBoundingRect();
    const finalSize = numberOr(object.fontSize, originalSize);
    if (serialized && finalSize < originalSize - 0.01) serialized.fontSize = finalSize;
    const label = String(serialized?.name || serialized?.id || '未命名图层');
    const remainsOutside = isOverflowing(rect);
    if (finalSize >= originalSize - 0.01 && !remainsOutside) return { warnings: [], blocked: false };
    return {
      warnings: [`图层“${label}”超出${row.name}${face.name}安全区域${finalSize < originalSize - 0.01 ? `，字号已从 ${originalSize.toFixed(2)} 缩小至 ${finalSize.toFixed(2)}` : ''}${remainsOutside ? '，缩小至最小可用字号后仍有超出，已阻止导出' : ''}`],
      blocked: remainsOutside,
    };
  }

  function buildTextSafeRegions(workarea, bounds, options = {}) {
    const unit = String(workarea.unit || '').toLowerCase();
    const factor = PIXELS_PER_UNIT[unit] || PIXELS_PER_UNIT.in;
    const bleed = Math.max(0, numberOr(workarea.bleed, 0));
    const horizontalBleed = Math.max(0, numberOr(workarea.separateBleed === true ? workarea.horizontalBleed : bleed, bleed));
    const verticalBleed = Math.max(0, numberOr(workarea.separateBleed === true ? workarea.verticalBleed : bleed, bleed));
    const sideWidth = Math.max(0, numberOr(workarea.sideWidth, 0));
    const sideHeight = Math.max(0, numberOr(workarea.sideHeight, 0));
    const logicalWidth = positiveOrNull(workarea.workareaWidth) || bounds.width;
    const rows = workarea.canvasRows === 2 ? 2 : 1;
    const spineWidth = rows === 2 ? 0 : Math.max(0, numberOr(workarea.spineWidth, 0));
    const spineBleed = rows === 2 ? 0 : Math.max(0, numberOr(workarea.spineBleed, 0));
    const rowGap = rows === 2 ? Math.max(0, numberOr(workarea.canvasRowGap, CANVAS_ROW_GAP)) : 0;
    const physicalWidth = sideWidth * 2 + horizontalBleed * 2 + spineBleed * 2 + spineWidth;
    const physicalRowHeight = sideHeight + verticalBleed * 2;
    const physicalHeight = physicalRowHeight * rows + rowGap / factor;
    const scaleX = physicalWidth > 0 ? bounds.width / (physicalWidth * factor) : 1;
    const scaleY = physicalHeight > 0 ? bounds.height / (physicalHeight * factor) : 1;
    // Single-row layouts keep the existing cover/spine margins. For a
    // double-row layout, the 6mm safe distance applies to each page edge;
    // there is no spine region in this layout.
    const configured = options
      && options.safeDistances !== null
      && typeof options.safeDistances === 'object'
      ? options.safeDistances
      : {};
    const readMargins = (camelKey, snakeKey, snakeJsonKey, fallback) => {
      // Backend product records use snake_case; direct renderer callers and
      // the frontend use camelCase. Product options win over document fields.
      const value = configured[camelKey]
        ?? configured[snakeKey]
        ?? configured[snakeJsonKey]
        ?? workarea[camelKey]
        ?? workarea[snakeKey]
        ?? workarea[snakeJsonKey]
        ?? {};
      const configuredFactor = PIXELS_PER_UNIT.mm;
      const resolveMargin = (side) => (
        value[side] == null
          ? fallback * factor
          : numberOr(value[side], 0) * configuredFactor
      );
      return {
        top: Math.max(0, resolveMargin('top')),
        right: Math.max(0, resolveMargin('right')),
        bottom: Math.max(0, resolveMargin('bottom')),
        left: Math.max(0, resolveMargin('left')),
      };
    };
    const doubleRowSafeMargin = 6 / 25.4;
    const coverMargin = readMargins(
      'coverSafeDistance',
      'cover_safe_distance',
      'cover_safe_distance_json',
      rows === 2 ? doubleRowSafeMargin : 0.4,
    );
    const backCoverMargin = readMargins(
      'backCoverSafeDistance',
      'back_cover_safe_distance',
      'back_cover_safe_distance_json',
      rows === 2 ? doubleRowSafeMargin : 0.4,
    );
    const spineMargin = readMargins(
      'spineSafeDistance',
      'spine_safe_distance',
      'spine_safe_distance_json',
      rows === 2 ? 0 : 0.05,
    );
    const makeRegion = (name, start, end) => {
      const actualStart = start * (name.startsWith('第') ? scaleY : scaleX);
      const actualEnd = end * (name.startsWith('第') ? scaleY : scaleX);
      return { name, start: actualStart, end: actualEnd };
    };
    const faces = rows === 2
      ? [
        makeRegion('封面', horizontalBleed * factor, (horizontalBleed + sideWidth) * factor),
        makeRegion('封底', (logicalWidth / factor - horizontalBleed - sideWidth) * factor, (logicalWidth / factor - horizontalBleed) * factor),
      ]
      : [
        makeRegion('封面', horizontalBleed * factor, (horizontalBleed + sideWidth) * factor),
        makeRegion('背脊', (horizontalBleed + sideWidth + spineBleed) * factor, (horizontalBleed + sideWidth + spineBleed + spineWidth) * factor),
        makeRegion('封底', (horizontalBleed + sideWidth + spineBleed + spineWidth + spineBleed) * factor, (logicalWidth / factor - horizontalBleed) * factor),
      ];
    const rowHeight = Math.max(1, (bounds.height - rowGap * (rows - 1)) / rows);
    const rowRegions = Array.from({ length: rows }, (_, row) => {
      const start = row * (rowHeight + rowGap) + verticalBleed * factor * scaleY;
      const end = row * (rowHeight + rowGap) + rowHeight - verticalBleed * factor * scaleY;
      const actualStart = start;
      const actualEnd = end;
      return { name: rows === 2 ? `第${row + 1}排` : '画布', start: actualStart, end: actualEnd };
    });
    return {
      faces,
      rows: rowRegions,
      coverMargin,
      backCoverMargin,
      spineMargin,
      scaleX,
      scaleY,
    };
  }

  function insetRegion(start, end, startMargin, endMargin) {
    const available = Math.max(0, end - start - 1);
    const total = startMargin + endMargin;
    if (total > available && total > 0) {
      const ratio = available / total;
      startMargin *= ratio;
      endMargin *= ratio;
    }
    return [start + startMargin, Math.max(start + 1, end - endMargin)];
  }

  function nearestRegion(value, regions) {
    const inside = regions.find(region => value >= region.start && value <= region.end);
    if (inside) return inside;
    return regions.reduce((nearest, region) => {
      const distance = value < region.start ? region.start - value : value - region.end;
      const nearestDistance = value < nearest.start ? nearest.start - value : value - nearest.end;
      return distance < nearestDistance ? region : nearest;
    });
  }

  function normalizeSafeDistance(value) {
    let raw = value;
    if (typeof raw === 'string' && raw.trim()) {
      try { raw = JSON.parse(raw); } catch { raw = {}; }
    }
    const source = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
    return {
      top: nonnegativeOrNull(source.top) ?? 0,
      right: nonnegativeOrNull(source.right) ?? 0,
      bottom: nonnegativeOrNull(source.bottom) ?? 0,
      left: nonnegativeOrNull(source.left) ?? 0,
    };
  }

  function resolveSafeDistances(options, source, workarea) {
    const sourceSafe = source
      && source.safeDistances !== null
      && typeof source.safeDistances === 'object'
      ? source.safeDistances
      : {};
    const optionSafe = options
      && options.safeDistances !== null
      && typeof options.safeDistances === 'object'
      ? options.safeDistances
      : {};
    const read = (camel, snake, snakeJson) => {
      const raw = optionSafe[camel]
        ?? optionSafe[snake]
        ?? optionSafe[snakeJson]
        ?? sourceSafe[camel]
        ?? sourceSafe[snake]
        ?? sourceSafe[snakeJson]
        ?? source?.[camel]
        ?? source?.[snake]
        ?? source?.[snakeJson]
        ?? workarea?.[camel]
        ?? workarea?.[snake]
        ?? workarea?.[snakeJson];
      // Leave an absent configuration absent so the backend's compatibility
      // defaults (rather than an injected all-zero object) still apply.
      return raw == null ? undefined : normalizeSafeDistance(raw);
    };
    return {
      coverSafeDistance: read('coverSafeDistance', 'cover_safe_distance', 'cover_safe_distance_json'),
      spineSafeDistance: read('spineSafeDistance', 'spine_safe_distance', 'spine_safe_distance_json'),
      backCoverSafeDistance: read('backCoverSafeDistance', 'back_cover_safe_distance', 'back_cover_safe_distance_json'),
    };
  }

  function buildPrintGuides(width, height, values, fallbackGuides) {
    if (!width || values.sideWidth === null || values.spineWidth === null || values.spineBleed === null) {
      return Array.isArray(fallbackGuides) ? fallbackGuides : [];
    }

    const sideWidth = values.sideWidth * values.factor;
    const horizontalBleed = values.horizontalBleed * values.factor;
    const verticalBleed = values.verticalBleed * values.factor;
    const rows = values.canvasRows === 2 ? 2 : 1;
    const spineWidth = rows === 2 ? 0 : values.spineWidth * values.factor;
    const spineBleed = rows === 2 ? 0 : values.spineBleed * values.factor;
    const verticalPositions = rows === 2
      ? [
        [0, 'bleed'],
        [horizontalBleed, 'content'],
        [horizontalBleed + sideWidth, 'content'],
        [width - horizontalBleed - sideWidth, 'content'],
        [width - horizontalBleed, 'content'],
        [width, 'bleed'],
      ]
      : [
        [0, 'bleed'],
        [horizontalBleed, 'content'],
        [horizontalBleed + sideWidth, 'bleed'],
        [horizontalBleed + sideWidth + spineBleed, 'content'],
        [horizontalBleed + sideWidth + spineBleed + spineWidth, 'content'],
        [horizontalBleed + sideWidth + spineBleed + spineWidth + spineBleed, 'bleed'],
        [width - horizontalBleed, 'content'],
        [width, 'bleed'],
      ];
    const rowGap = rows === 2
      ? (nonnegativeOrNull(values.canvasRowGap) ?? CANVAS_ROW_GAP)
      : 0;
    const rowHeight = Math.max(1, (height - rowGap * (rows - 1)) / rows);
    const guides = [];
    const add = (orientation, position, kind) => {
      const limit = orientation === 'vertical' ? width : height;
      if (position < 0 || position > limit) return;
      if (guides.some(guide => guide.orientation === orientation && Math.abs(guide.position - position) < 0.01)) return;
      guides.push({ orientation, position, kind });
    };

    verticalPositions.forEach(([position, kind]) => add('vertical', position, kind));
    for (let row = 0; row < rows; row += 1) {
      const rowTop = row * (rowHeight + rowGap);
      const rowBottom = rowTop + rowHeight;
      add('horizontal', rowTop, 'bleed');
      add('horizontal', rowTop + verticalBleed, 'content');
      add('horizontal', rowBottom - verticalBleed, 'content');
      add('horizontal', rowBottom, 'bleed');
    }
    return guides;
  }

  function resolveWorkareaBounds(workarea) {
    const angle = Number(workarea.angle || 0);
    if (angle !== 0) throw new Error('不支持旋转后的 workarea');
    const scaledWidth = finiteOrNull(workarea.width) !== null
      ? Math.abs(Number(workarea.width) * numberOr(workarea.scaleX, 1))
      : null;
    const scaledHeight = finiteOrNull(workarea.height) !== null
      ? Math.abs(Number(workarea.height) * numberOr(workarea.scaleY, 1))
      : null;
    const width = positive(scaledWidth || workarea.workareaWidth, 'workarea.width');
    const height = positive(scaledHeight || workarea.workareaHeight, 'workarea.height');
    const left = finite(workarea.left, 'workarea.left') - originOffset(workarea.originX, width, 'x');
    const top = finite(workarea.top, 'workarea.top') - originOffset(workarea.originY, height, 'y');
    return { left, top, width, height };
  }

  function toWorkareaScene(serialized, bounds) {
    const local = clone(serialized);
    if (finiteOrNull(local.left) !== null) local.left = Number(local.left) - bounds.left;
    if (finiteOrNull(local.top) !== null) local.top = Number(local.top) - bounds.top;
    if (String(local.id || '').toLowerCase() === 'workarea') {
      local.selectable = false;
      local.evented = false;
    }
    if (/^https?:/i.test(String(local.src || '')) && local.crossOrigin == null) {
      local.crossOrigin = 'anonymous';
    }
    return local;
  }

  function collectFontSources(objects, options = {}) {
    const fonts = new Map();
    visit(objects, object => {
      const family = String(object.fontFamily || '').trim();
      const url = String(object.fontUrl || object.font_url || '').trim();
      const loadUrl = String(object.fontDataUrl || '').trim() || url;
      if (!family || !url) return;
      const weight = object.fontWeight || 'normal';
      const style = object.fontStyle || 'normal';
      const key = `${family}\u0000${url}\u0000${weight}\u0000${style}`;
      fonts.set(key, { family, url, loadUrl, weight, style });
    });
    return [...fonts.values()].map(font => options.textToSvg
      ? { family: font.family, url: font.url, loadUrl: font.loadUrl }
      : font);
  }

  function layerNames(objects) {
    return new Map(objects.map(object => [
      String(object?.id || ''),
      String(object?.name || (String(object?.id || '').toLowerCase() === 'workarea' ? '画布' : object?.id) || object?.type || '图层'),
    ]));
  }

  function renderedPrintGuides(workarea, bounds) {
    const guides = Array.isArray(workarea.printGuides) ? workarea.printGuides : [];
    const logicalWidth = numberOr(workarea.workareaWidth, bounds.width) || bounds.width;
    const logicalHeight = numberOr(workarea.workareaHeight, bounds.height) || bounds.height;
    const scaleX = bounds.width / logicalWidth;
    const scaleY = bounds.height / logicalHeight;
    return guides.map(guide => ({
      ...guide,
      position: (Number(guide.position) || 0) * (guide.orientation === 'vertical' ? scaleX : scaleY),
    }));
  }

  function exportOptions(canvas, workarea, bounds, objects, textToSvg = false) {
    const rawSvg = canvas.toSVG({
      width: bounds.width,
      height: bounds.height,
      viewBox: { x: 0, y: 0, width: bounds.width, height: bounds.height },
    });
    return {
      rawSvg,
      bounds: { left: 0, top: 0, width: bounds.width, height: bounds.height },
      backgroundColor: String(workarea.backgroundColor || '#ffffff'),
      layerNames: layerNames(objects),
      fontSources: collectFontSources(objects, { textToSvg }),
      printGuides: renderedPrintGuides(workarea, bounds),
    };
  }

  function buildSvg(canvas, workarea, bounds, objects) {
    const exporter = global.ImageMapEditorSvgExport;
    if (!exporter?.exportCorelCompatibleSvg) throw new Error('编辑器 SVG 导出模块未加载');
    return exporter.exportCorelCompatibleSvg(exportOptions(canvas, workarea, bounds, objects));
  }

  async function buildTextToSvg(canvas, workarea, bounds, objects) {
    const exporter = global.ImageMapEditorSvgExport;
    if (!exporter?.exportTextToSvg) throw new Error('编辑器 text-to-svg 导出模块未加载');
    return exporter.exportTextToSvg(exportOptions(canvas, workarea, bounds, objects, true));
  }

  async function createObject(serialized, warnings) {
    const type = normalizeType(serialized.type);
    if (type === 'textbox') return createNoWrapText(serialized);
    if (type === 'svg') return createSvg(serialized);
    if (type === 'arrow') return createArrow(serialized);
    if (type === 'cube') return createCube(serialized);
    if (type === 'gif') return createImageLike(serialized, warnings);
    if (['chart', 'element', 'iframe', 'video'].includes(type)) {
      warnings.push(`对象 ${serialized.id || '<无 id>'} (${serialized.type}) 是 DOM 图层，静态画布导出时不绘制`);
      return null;
    }
    try {
      const objects = await fabric.util.enlivenObjects([serialized]);
      if (!objects.length) throw new Error('Fabric 未返回对象');
      return objects[0];
    } catch (error) {
      throw new Error(`无法还原对象 ${serialized.id || '<无 id>'} (${serialized.type || '未知类型'}): ${error.message}`);
    }
  }

  function createNoWrapText(serialized) {
    const options = without(serialized, ['type', 'text', 'width', 'height']);
    // FabricText only breaks on explicit newline characters. Keeping left/top
    // and origin preserves the authored anchor while the natural width grows.
    return new fabric.FabricText(String(serialized.text || ''), options);
  }

  async function createImageLike(serialized, warnings) {
    if (!serialized.src) {
      warnings.push(`对象 ${serialized.id || '<无 id>'} (${serialized.type}) 缺少 src，已忽略`);
      return null;
    }
    const image = await fabric.FabricImage.fromURL(String(serialized.src), {
      crossOrigin: serialized.crossOrigin || 'anonymous',
    });
    const options = without(serialized, ['type']);
    image.set(options);
    return image;
  }

  async function createSvg(serialized) {
    const source = serialized.svg || serialized.src;
    if (!source) throw new Error(`SVG 对象 ${serialized.id || '<无 id>'} 缺少 svg/src`);
    const inline = String(serialized.loadType || '').toLowerCase() === 'svg' || /^\s*</.test(String(source));
    const parsed = inline
      ? await fabric.loadSVGFromString(String(source))
      : await fabric.loadSVGFromURL(String(source), { crossOrigin: 'anonymous' });
    const children = (parsed.objects || []).filter(Boolean);
    const grouped = fabric.util.groupSVGElements(children, parsed.options || {});
    const desiredHeight = Number(serialized.height) * numberOr(serialized.scaleY, 1);
    const sourceHeight = Number(grouped.height) || 1;
    const scale = Number.isFinite(desiredHeight) && desiredHeight > 0
      ? desiredHeight / sourceHeight
      : numberOr(grouped.scaleY, 1);
    const options = without(serialized, ['type', 'objects', 'layoutManager']);
    grouped.set({ ...options, scaleX: scale, scaleY: scale });
    if (serialized.fill || serialized.stroke) {
      const items = typeof grouped.getObjects === 'function' ? grouped.getObjects() : [grouped];
      for (const item of items) {
        if (serialized.fill) item.set('fill', serialized.fill);
        if (serialized.stroke) item.set('stroke', serialized.stroke);
      }
    }
    return grouped;
  }

  function createArrow(serialized) {
    const points = [serialized.x1 || 0, serialized.y1 || 0, serialized.x2 || 0, serialized.y2 || 0];
    const object = new fabric.Line(points, without(serialized, ['type']));
    const renderLine = object._render.bind(object);
    object._render = function renderArrow(context) {
      renderLine(context);
      context.save();
      const angle = Math.atan2(this.y2 - this.y1, this.x2 - this.x1);
      context.translate((this.x2 - this.x1) / 2, (this.y2 - this.y1) / 2);
      context.rotate(angle);
      context.beginPath();
      context.moveTo(5, 0);
      context.lineTo(-5, 5);
      context.lineTo(-5, -5);
      context.closePath();
      context.fillStyle = this.stroke || this.fill || '#000000';
      context.fill();
      context.restore();
    };
    return object;
  }

  function createCube(serialized) {
    const object = new fabric.FabricObject(without(serialized, ['type']));
    object._render = function renderCube(context) {
      const fill = String(this.fill || '#000000');
      const width = Number(this.width) || 0;
      const height = Number(this.height) || 0;
      const wx = width / 2;
      const wy = width / 2;
      const h = height / 2;
      const draw = (points, face, stroke) => {
        context.beginPath();
        points.forEach(([x, y], index) => index ? context.lineTo(x, y) : context.moveTo(x, y));
        context.closePath();
        context.fillStyle = face;
        context.strokeStyle = stroke;
        context.stroke();
        context.fill();
      };
      draw([[0, wy], [-wx, wy - wx * 0.5], [-wx, wy - h - wx * 0.5], [0, wy - h]], shade(fill, -10), fill);
      draw([[0, wy], [wy, wy - wy * 0.5], [wy, wy - h - wy * 0.5], [0, wy - h]], shade(fill, 10), shade(fill, 50));
      draw([[0, wy - h], [-wx, wy - h - wx * 0.5], [-wx + wy, wy - h - (wx + wy) * 0.5], [wy, wy - h - wy * 0.5]], shade(fill, 20), shade(fill, 60));
    };
    return object;
  }

  async function loadFonts(objects, warnings) {
    const fonts = new Map();
    visit(objects, object => {
      const family = String(object.fontFamily || '').trim();
      const url = String(object.fontDataUrl || object.fontUrl || object.font_url || '').trim();
      if (!family || !url) return;
      const weight = String(object.fontWeight || 'normal');
      const style = String(object.fontStyle || 'normal');
      fonts.set(`${family}\u0000${weight}\u0000${style}`, { family, url, weight, style });
    });
    for (const font of fonts.values()) {
      try {
        const face = new FontFace(font.family, `url(${JSON.stringify(font.url)})`, {
          weight: font.weight,
          style: font.style,
        });
        await face.load();
        global.document.fonts.add(face);
      } catch (error) {
        warnings.push(`字体 ${font.family} 加载失败: ${error.message}`);
      }
    }
    await global.document.fonts.ready;
  }

  function visit(objects, callback) {
    for (const object of objects) {
      if (!object || typeof object !== 'object') continue;
      callback(object);
      if (Array.isArray(object.objects)) visit(object.objects, callback);
    }
  }

  function installTextWordSpacingSupport() {
    const prototype = fabric.FabricText?.prototype;
    if (!prototype || prototype.wordSpacingPatchInstalled) return;
    const getGraphemeBox = prototype._getGraphemeBox;
    const renderChars = prototype._renderChars;
    prototype._getGraphemeBox = function getWordSpacedBox(grapheme, ...args) {
      const box = getGraphemeBox.call(this, grapheme, ...args);
      const spacing = Number(this.wordSpacing) || 0;
      if (spacing !== 0 && /\s/u.test(grapheme)) {
        const width = (this.fontSize * spacing) / 1000;
        box.width += width;
        box.kernedWidth += width;
      }
      return box;
    };
    prototype._renderChars = function renderWordSpacedChars(...args) {
      if ((Number(this.wordSpacing) || 0) === 0 || this.charSpacing !== 0) return renderChars.apply(this, args);
      this.charSpacing = Number.EPSILON;
      try {
        return renderChars.apply(this, args);
      } finally {
        this.charSpacing = 0;
      }
    };
    prototype.wordSpacingPatchInstalled = true;
  }

  function normalizeFormats(value) {
    const formats = Array.isArray(value) && value.length ? value : ['jpg', 'png'];
    const normalized = [...new Set(formats.map(item => String(item).toLowerCase() === 'jpeg' ? 'jpg' : String(item).toLowerCase()))];
    if (normalized.some(item => !['jpg', 'png', 'svg', 'text-to-svg'].includes(item))) throw new Error('仅支持 jpg、png、svg 和 text-to-svg');
    return normalized;
  }

  function normalizeType(value) {
    return String(value || '').replace(/[-_]/g, '').toLowerCase();
  }

  function originOffset(origin, size, axis) {
    const value = String(origin || (axis === 'x' ? 'left' : 'top')).toLowerCase();
    if (value === 'center' || value === 'middle') return size / 2;
    if (value === 'right' || value === 'bottom') return size;
    return 0;
  }

  function shade(color, percent) {
    const match = /^#?([0-9a-f]{6})$/i.exec(color);
    if (!match) return color;
    const amount = Math.round(2.55 * percent);
    const number = parseInt(match[1], 16);
    const channel = shift => Math.max(0, Math.min(255, (number >> shift & 0xff) + amount));
    return `rgb(${channel(16)},${channel(8)},${channel(0)})`;
  }

  function without(value, keys) {
    const copy = { ...value };
    keys.forEach(key => delete copy[key]);
    return copy;
  }

  function clone(value) {
    if (value == null) throw new Error('缺少图层 JSON');
    return typeof structuredClone === 'function'
      ? structuredClone(value)
      : JSON.parse(JSON.stringify(value));
  }

  function positive(value, name) {
    const number = Number(value);
    if (!Number.isFinite(number) || number <= 0) throw new Error(`${name} 必须大于 0`);
    return number;
  }

  function finite(value, name) {
    const number = Number(value);
    if (!Number.isFinite(number)) throw new Error(`${name} 必须是有效数字`);
    return number;
  }

  function finiteOrNull(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function nonnegativeOrNull(value) {
    const number = finiteOrNull(value);
    return number !== null && number >= 0 ? number : null;
  }

  function positiveOrNull(value) {
    const number = finiteOrNull(value);
    return number !== null && number > 0 ? number : null;
  }

  function numberOr(value, fallback) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function bounded(value, min, max, name) {
    const number = Number(value);
    if (!Number.isFinite(number) || number < min || number > max) {
      throw new Error(`${name} 必须在 ${min} 到 ${max} 之间`);
    }
    return number;
  }
})(globalThis);
