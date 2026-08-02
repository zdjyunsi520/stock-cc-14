// Philippines: Create Promo
import { sendToTab, findTab, waitForTab, sleep } from './tab.js';
import { fmtDatePH, fmtTime24, parseDateTime } from './time.js';

export async function createPromoPH(count) {
  const log = (msg) => console.log(`[PH-Promo] ${msg}`);
  let created = 0;

  for (let round = 0; round < count; round++) {
    log(`=== Round ${round + 1}/${count} ===`);

    let listTab = await findTab(/tiktokshopglobalselling.*management/);
    if (!listTab) throw new Error('请先打开菲律宾促销列表页');
    log(`list tab: ${listTab.id}`);

    const queryRes = await sendToTab(listTab.id, {
      type: 'execute', actions: [{ type: 'query', selector: 'span.text-gray-500' }]
    });
    const times = queryRes?.results?.[0]?.items;
    if (!times || times.length < 2) throw new Error('未找到促销时间');
    const endTimeStr = times[1].text;
    log(`end time: ${endTimeStr}`);

    const endMoment = parseDateTime(endTimeStr);
    const newStart = new Date(endMoment.getTime() + 60000);
    const newEnd = new Date(newStart.getTime() + 3 * 86400000);
    log(`new: ${fmtDatePH(newStart)} ${fmtTime24(newStart)} ~ ${fmtDatePH(newEnd)} ${fmtTime24(newEnd)}`);

    await sendToTab(listTab.id, {
      type: 'execute', actions: [
        { type: 'click', selector: 'td.theme-arco-table-td.theme-arco-table-col-fixed-right > div.theme-arco-table-cell > span.theme-arco-table-cell-wrap-value > div.flex.w-min > button.theme-arco-btn.theme-arco-btn-secondary' }
      ]
    });
    log('clicked Duplicate');

    const createTab = await waitForTab(/flash-sale\/create/, 15000);
    log(`create tab: ${createTab.id}`);

    for (let attempt = 0; attempt < 10; attempt++) {
      try {
        const check = await sendToTab(createTab.id, {
          type: 'execute', actions: [{ type: 'query', selector: 'input[placeholder="Start time"]' }]
        });
        if (check?.results?.[0]?.count > 0) { log('page ready'); break; }
      } catch (e) {}
      log('waiting for page load... ' + (attempt + 1));
      await sleep(2000);
    }
    await sleep(3000);

    await sendToTab(createTab.id, {
      type: 'execute', actions: [
        { type: 'click', selector: 'input[placeholder="Start time"]' },
        { type: 'wait', value: 500 },
        { type: 'type', selector: 'input[placeholder="Start time"]', value: fmtDatePH(newStart) },
        { type: 'wait', value: 500 },
        { type: 'press', selector: 'input[placeholder="Start time"]', key: 'Enter' }
      ]
    });
    await sleep(500);

    await sendToTab(createTab.id, {
      type: 'execute', actions: [
        { type: 'click', selector: 'input[placeholder="Select time"]', index: 0 },
        { type: 'wait', value: 500 },
        { type: 'type', selector: 'input[placeholder="Select time"]', index: 0, value: fmtTime24(newStart) },
        { type: 'wait', value: 500 },
        { type: 'press', selector: 'input[placeholder="Select time"]', index: 0, key: 'Enter' }
      ]
    });
    await sleep(500);

    await sendToTab(createTab.id, {
      type: 'execute', actions: [
        { type: 'click', selector: 'input[placeholder="End time"]' },
        { type: 'wait', value: 500 },
        { type: 'type', selector: 'input[placeholder="End time"]', value: fmtDatePH(newEnd) },
        { type: 'wait', value: 500 },
        { type: 'press', selector: 'input[placeholder="End time"]', key: 'Enter' }
      ]
    });
    await sleep(500);

    await sendToTab(createTab.id, {
      type: 'execute', actions: [
        { type: 'click', selector: 'input[placeholder="Select time"]', index: 1 },
        { type: 'wait', value: 500 },
        { type: 'type', selector: 'input[placeholder="Select time"]', index: 1, value: fmtTime24(newEnd) },
        { type: 'wait', value: 500 },
        { type: 'press', selector: 'input[placeholder="Select time"]', index: 1, key: 'Enter' }
      ]
    });
    await sleep(500);
    log('all times set');

    for (let submitAttempt = 0; submitAttempt < 3; submitAttempt++) {
      try {
        await sendToTab(createTab.id, {
          type: 'execute', actions: [
            { type: 'scroll', value: -99999 },
            { type: 'wait', value: 500 },
            { type: 'click', selector: 'div.flex.bg-white > div.flex.justify-between > div.flex.w-full > div > button.theme-arco-btn-primary' },
            { type: 'wait', value: 3000 },
            { type: 'click', selector: 'div.theme-arco-modal button.theme-arco-btn-primary' }
          ]
        }, 30000);
      } catch (e) {
        log('submit response lost (page navigated), this is normal');
      }
      await sleep(3000);
      const checkTab = await findTab(/flash-sale\/create/);
      if (!checkTab) { log('submit success, page navigated'); break; }
      log('submit may have failed, retry ' + (submitAttempt + 1));
    }
    log('submitted');

    created++;
    log(`created ${created}/${count}`);

    if (round < count - 1) {
      log('waiting for list page...');
      await sleep(5000);
      for (let attempt = 0; attempt < 20; attempt++) {
        try {
          const listTab2 = await findTab(/tiktokshopglobalselling.*management/);
          if (listTab2) {
            await sendToTab(listTab2.id, { type: 'ping' }, 5000);
            log('list page ready');
            break;
          }
        } catch (e) {
          log('waiting... attempt ' + (attempt + 1));
          await sleep(2000);
        }
      }
    }
  }

  return { created };
}
