import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../data/api/docs_api.dart';
import 'reader_controller.dart';
import 'widgets/section_view.dart';
import 'widgets/toc_drawer.dart';

/// 章节阅读器（M5.3）。
///
/// 行为锚：`docs/03-development/05-frontend.md §6`：
/// - `/reader/{spec}`：左 章节树 drawer + 中央 spec 概览
/// - `/reader/{spec}/{section}`：左 章节树 drawer + 中央 [SectionView]
/// - `/reader/{spec}/{section}#chunk-{id}`：进入 section 后高亮该 chunk 3s
///
/// 响应式：宽屏（>= 840）固定左 drawer + 主区；窄屏 AppBar 抽屉化。
class ReaderPage extends ConsumerStatefulWidget {
  const ReaderPage({
    super.key,
    required this.specId,
    this.sectionPath,
    this.activeChunkId,
  });

  final String specId;
  final String? sectionPath;
  final String? activeChunkId;

  static const double wideBreakpoint = 840;

  @override
  ConsumerState<ReaderPage> createState() => _ReaderPageState();
}

class _ReaderPageState extends ConsumerState<ReaderPage> {
  final GlobalKey<ScaffoldState> _scaffoldKey = GlobalKey<ScaffoldState>();

  void _onSelectSection(SectionNode node) {
    final spec = Uri.encodeComponent(widget.specId);
    final sec = Uri.encodeComponent(node.joinedPath);
    _closeDrawerIfOpen();
    GoRouter.of(context).go('/reader/$spec/$sec');
  }

  void _onSelectChunk(SearchHit hit) {
    final spec = Uri.encodeComponent(widget.specId);
    final sec = Uri.encodeComponent(hit.joinedPath);
    _closeDrawerIfOpen();
    GoRouter.of(context).go('/reader/$spec/$sec#chunk-${hit.chunkId}');
  }

  void _closeDrawerIfOpen() {
    final state = _scaffoldKey.currentState;
    if (state != null && state.isDrawerOpen) {
      Navigator.of(state.context).pop();
    }
  }

  @override
  Widget build(BuildContext context) {
    final drawer = TocDrawer(
      specId: widget.specId,
      currentSectionPath: widget.sectionPath,
      onSelectSection: _onSelectSection,
      onSelectChunk: _onSelectChunk,
    );

    final main = widget.sectionPath == null || widget.sectionPath!.isEmpty
        ? _SpecOverview(specId: widget.specId)
        : SectionView(
            key: ValueKey('section-${widget.specId}-${widget.sectionPath}-${widget.activeChunkId}'),
            specId: widget.specId,
            sectionPath: widget.sectionPath!,
            activeChunkId: widget.activeChunkId,
          );

    return LayoutBuilder(builder: (ctx, constraints) {
      final isWide = constraints.maxWidth >= ReaderPage.wideBreakpoint;
      if (isWide) {
        return Scaffold(
          key: _scaffoldKey,
          appBar: _ReaderAppBar(
            specId: widget.specId,
            sectionPath: widget.sectionPath,
            showMenu: false,
            onMenu: () {},
          ),
          body: Row(
            children: [
              SizedBox(
                width: 300,
                child: Material(
                  color: Theme.of(context).colorScheme.surface,
                  child: drawer,
                ),
              ),
              const VerticalDivider(width: 1, thickness: 1),
              Expanded(child: main),
            ],
          ),
        );
      }
      return Scaffold(
        key: _scaffoldKey,
        appBar: _ReaderAppBar(
          specId: widget.specId,
          sectionPath: widget.sectionPath,
          showMenu: true,
          onMenu: () => _scaffoldKey.currentState?.openDrawer(),
        ),
        drawer: Drawer(width: 300, child: SafeArea(child: drawer)),
        body: main,
      );
    });
  }
}

class _ReaderAppBar extends StatelessWidget implements PreferredSizeWidget {
  const _ReaderAppBar({
    required this.specId,
    required this.sectionPath,
    required this.showMenu,
    required this.onMenu,
  });

  final String specId;
  final String? sectionPath;
  final bool showMenu;
  final VoidCallback onMenu;

  @override
  Size get preferredSize => const Size.fromHeight(kToolbarHeight);

  @override
  Widget build(BuildContext context) {
    final crumb = sectionPath == null || sectionPath!.isEmpty
        ? specId
        : '$specId · §$sectionPath';
    return AppBar(
      leading: showMenu
          ? IconButton(
              key: const Key('reader_open_drawer'),
              icon: const Icon(Icons.menu_book_outlined),
              tooltip: '章节目录',
              onPressed: onMenu,
            )
          : IconButton(
              key: const Key('reader_back_chat'),
              icon: const Icon(Icons.arrow_back),
              tooltip: '回到会话',
              onPressed: () => GoRouter.of(context).go('/chat'),
            ),
      title: Text(
        crumb,
        key: const Key('reader_crumb'),
        overflow: TextOverflow.ellipsis,
      ),
    );
  }
}

class _SpecOverview extends ConsumerWidget {
  const _SpecOverview({required this.specId});

  final String specId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(docDetailProvider(specId));
    return async.when(
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (e, _) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Text(
            '加载 spec 失败：$e',
            key: const Key('spec_overview_error'),
            textAlign: TextAlign.center,
          ),
        ),
      ),
      data: (d) => Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              d.specId,
              key: const Key('spec_overview_id'),
              style: Theme.of(context).textTheme.headlineMedium,
            ),
            const SizedBox(height: 4),
            Text(
              '${d.release} · series ${d.series} · ${d.sections.length} sections',
              style: Theme.of(context).textTheme.bodyMedium,
            ),
            const SizedBox(height: 20),
            Expanded(
              child: ListView.builder(
                key: const Key('spec_overview_sections'),
                itemCount: d.sections.length,
                itemBuilder: (ctx, i) {
                  final s = d.sections[i];
                  return ListTile(
                    dense: true,
                    title: Text('§${s.joinedPath}  ${s.sectionTitle}'),
                    subtitle: Text('${s.chunkCount} chunks'),
                    onTap: () {
                      final spec = Uri.encodeComponent(d.specId);
                      final sec = Uri.encodeComponent(s.joinedPath);
                      GoRouter.of(context).go('/reader/$spec/$sec');
                    },
                  );
                },
              ),
            ),
          ],
        ),
      ),
    );
  }
}
