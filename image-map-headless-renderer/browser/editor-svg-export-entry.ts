import { exportCorelCompatibleSvg } from '../../src/image-map-editor/canvas/utils/exportCorelCompatibleSvg';
import { exportTextToSvg } from '../../src/image-map-editor/canvas/utils/exportTextToSvg';

const target = globalThis as typeof globalThis & {
	ImageMapEditorSvgExport?: {
		exportCorelCompatibleSvg: typeof exportCorelCompatibleSvg;
		exportTextToSvg: typeof exportTextToSvg;
	};
};

target.ImageMapEditorSvgExport = {
	exportCorelCompatibleSvg,
	exportTextToSvg,
};
